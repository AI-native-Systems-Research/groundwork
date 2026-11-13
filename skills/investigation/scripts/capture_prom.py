#!/usr/bin/env python3
"""Freeze a Prometheus query_range snapshot into a study (references/telemetry.md).

  capture_prom.py --study investigations/<slug> --capture-id <id>
                  (--url URL [--token-env VAR] | --port-forward CTX/NS/SVC[:PORT])
                  --start <rfc3339> --end <rfc3339> [--step 30s]
                  [--queries FILE] [--extra-queries FILE]

Writes <study>/telemetry/<id>/{capture.json, summary.json, prom/<name>.json,
metrics.jsonl}. Read-only against Prometheus, but it touches an external
system, so the skill only runs it after the owner's go.

HTTP is stdlib urllib. PyYAML is used for the query files (F2). The bearer
token is read from the named env var only; it is never printed, logged, or
written, and any message or response body that could contain it is redacted
first.

Exit codes: 0 captured (empty or failed single queries are recorded, not
fatal); 1 every query failed, or port-forward / network setup failed;
2 bad arguments (including a step over the per-series point limit).
"""
from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True  # an install inside a target repo must not dirty it (D4)
sys.path.insert(0, str(HERE))
import _config  # noqa: E402
from _telemetry import (CAPTURE_VERSION, MAX_POINTS_PER_SERIES,  # noqa: E402
                        SIZE_GUARD_BYTES, SUMMARY_VERSION, sha256_file,
                        summarize_query_range)

DEFAULT_QUERIES = HERE.parent / "assets" / "promql" / "starter.yaml"
DEFAULT_REMOTE_PORT = 9090
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
HTTP_TIMEOUT = float(os.environ.get("INVESTIGATION_PROM_TIMEOUT", "60"))
PF_READY_TIMEOUT = float(os.environ.get("INVESTIGATION_PF_TIMEOUT", "20"))
# Nice steps for the "suggested step" message, in seconds.
STEP_LADDER = [1, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600,
               7200, 10800, 21600, 43200, 86400]


class CaptureError(Exception):
    """A fatal, user-facing error. Messages never contain the token."""

    def __init__(self, msg: str, code: int = 1):
        super().__init__(msg)
        self.code = code


_SECRETS: list[str] = []


def redact(s: str) -> str:
    for secret in _SECRETS:
        if secret:
            s = s.replace(secret, "***")
    return s


def redact_bytes(b: bytes) -> bytes:
    """redact() for raw response bodies, before they are written to disk.

    An error body can echo the request, token included (a misbehaving gateway,
    a verbose proxy); the written prom/<name>.json must not carry it.
    """
    for secret in _SECRETS:
        if secret:
            b = b.replace(secret.encode(), b"***")
    return b


def err(msg: str) -> None:
    print(redact(f"capture_prom: {msg}"), file=sys.stderr)


# --- parsing ----------------------------------------------------------------

def parse_time(s: str, flag: str) -> dt.datetime:
    try:
        t = dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        raise CaptureError(f"{flag}: not an RFC 3339 time: {s!r}", 2)
    if t.tzinfo is None:
        raise CaptureError(f"{flag}: RFC 3339 needs a timezone (e.g. Z or +00:00): {s!r}", 2)
    return t.astimezone(dt.timezone.utc)


def rfc3339(t: dt.datetime) -> str:
    t = t.astimezone(dt.timezone.utc)
    s = t.isoformat(timespec="milliseconds" if t.microsecond else "seconds")
    return s.replace("+00:00", "Z")


_DUR_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|y|w|d|h|m|s)")
_DUR_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400,
              "w": 604800, "y": 31536000}


def parse_step(s: str) -> float:
    """Prometheus duration ('30s', '1m30s', '500ms') or plain seconds ('30')."""
    s = s.strip()
    try:
        secs = float(s)
    except ValueError:
        pos, secs = 0, 0.0
        for m in _DUR_RE.finditer(s):
            if m.start() != pos:
                break
            secs += float(m.group(1)) * _DUR_UNITS[m.group(2)]
            pos = m.end()
        if pos != len(s) or not s:
            raise CaptureError(f"--step: not a duration: {s!r} (e.g. 30s, 1m, 5m)", 2)
    if not math.isfinite(secs) or secs <= 0:
        raise CaptureError(f"--step must be positive: {s!r}", 2)
    return secs


def fmt_step(secs: float) -> str:
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= n and secs % n == 0:
            return f"{int(secs // n)}{unit}"
    return f"{int(secs)}s" if secs == int(secs) else f"{secs:g}s"


def suggest_step(duration: float) -> float:
    for s in STEP_LADDER:
        if duration / s <= MAX_POINTS_PER_SERIES:
            return float(s)
    return float(math.ceil(duration / MAX_POINTS_PER_SERIES / 86400) * 86400)


def check_step(start: dt.datetime, end: dt.datetime, step: float) -> None:
    """Same rule as Prometheus: (end - start) / step must be <= 11,000. Never clamps."""
    duration = (end - start).total_seconds()
    if duration / step > MAX_POINTS_PER_SERIES:
        points = int(duration // step) + 1
        sug = suggest_step(duration)
        raise CaptureError(
            f"--step {fmt_step(step)} over a {fmt_step(duration)} window gives {points} "
            f"points per series, above Prometheus' {MAX_POINTS_PER_SERIES} limit. "
            f"Suggested step: --step {fmt_step(sug)} (or capture a shorter window). "
            f"The step is never clamped silently.", 2)


def parse_port_forward(spec: str) -> dict:
    """CTX/NS/SVC[:PORT]. The context may itself contain '/' or ':' (EKS ARNs)."""
    parts = spec.rsplit("/", 2)
    if len(parts) != 3 or not all(parts):
        raise CaptureError(f"--port-forward: expected CTX/NS/SVC[:PORT], got {spec!r}", 2)
    ctx, ns, svc = parts
    port = DEFAULT_REMOTE_PORT
    if ":" in svc:
        svc, p = svc.rsplit(":", 1)
        if not p.isdigit() or not 0 < int(p) < 65536 or not svc:
            raise CaptureError(f"--port-forward: bad service port in {spec!r}", 2)
        port = int(p)
    return {"context": ctx, "namespace": ns, "service": svc, "port": port}


def is_loopback(host: str) -> bool:
    """True for localhost (any case) or any loopback IP (127.x.x.x, ::1)."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_url(url: str, allow_insecure_http: bool = False) -> tuple[str, str]:
    """Return (base URL for requests, host for capture.json). Rejects inline creds.

    Also refuses plain http to a non-loopback host (the bearer token would
    travel in cleartext) unless the caller explicitly acknowledged it.
    """
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise CaptureError("--url must be an http(s) URL with a host", 2)
    if u.username is not None or u.password is not None:
        raise CaptureError("--url must not carry credentials; put a bearer token in an "
                           "env var and pass --token-env VAR", 2)
    if u.query or u.fragment:
        raise CaptureError("--url must not carry a query string or fragment", 2)
    if u.scheme == "http" and not is_loopback(u.hostname) and not allow_insecure_http:
        raise CaptureError(f"--url uses plain http to non-loopback host {u.hostname}: "
                           "the bearer token would be sent in cleartext; use https, "
                           "or pass --allow-insecure-http to acknowledge it", 2)
    host = u.hostname if u.port is None else f"{u.hostname}:{u.port}"
    if ":" in (u.hostname or "") and u.port is None:
        host = f"[{u.hostname}]"
    base = urllib.parse.urlunsplit((u.scheme, u.netloc, u.path.rstrip("/"), "", ""))
    return base, host


def load_queries(path: Path, what: str) -> dict[str, str]:
    if not path.is_file():
        raise CaptureError(f"{what}: no such file: {path}", 2)
    try:
        data = _config.load_yaml(path)
    except _config.ConfigError as e:
        raise CaptureError(f"{what}: {e}", 2)
    if data is None:
        return {}
    return check_query_map(data, f"{what} {path}")


def check_query_map(data, where: str) -> dict[str, str]:
    if not isinstance(data, dict):
        raise CaptureError(f"{where}: must be a flat {{name: promql}} mapping", 2)
    out = {}
    for k, v in data.items():
        k = str(k)
        if not NAME_RE.match(k):
            raise CaptureError(f"{where}: query name {k!r} must match {NAME_RE.pattern}", 2)
        if not isinstance(v, str) or not v.strip():
            raise CaptureError(f"{where}: query {k!r} must be a non-empty PromQL string", 2)
        out[k] = v.strip()
    return out


# --- HTTP -------------------------------------------------------------------

def query_range(base: str, token: str | None, promql: str, start: dt.datetime,
                end: dt.datetime, step: float) -> tuple[bytes | None, dict | None, str | None]:
    """Return (raw body, parsed JSON, error). error is None on status=success."""
    body = urllib.parse.urlencode({
        "query": promql, "start": f"{start.timestamp():.3f}",
        "end": f"{end.timestamp():.3f}", "step": f"{step:g}",
    }).encode()
    req = urllib.request.Request(base + "/api/v1/query_range", data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        # Prometheus answers bad queries with 400/422 and a JSON error body.
        raw = e.read() or b""
        http_err = f"HTTP {e.code}"
        try:
            doc = json.loads(raw)
        except ValueError:
            return None, None, http_err
        if isinstance(doc, dict) and doc.get("status") == "error":
            return raw, doc, redact(f"{doc.get('errorType', http_err)}: {doc.get('error', '')}")
        return None, None, http_err
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        return None, None, redact(f"request failed: {type(reason).__name__}: {reason}")
    try:
        doc = json.loads(raw)
    except ValueError:
        return None, None, "response is not JSON (an auth proxy login page?)"
    if not isinstance(doc, dict):
        return None, None, "response is not a JSON object"
    if doc.get("status") != "success":
        return raw, doc, redact(f"{doc.get('errorType', 'error')}: {doc.get('error', '')}")
    return raw, doc, None


# --- port-forward -----------------------------------------------------------

def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


class PortForward:
    """`kubectl port-forward` on a random local port; always torn down on exit."""

    def __init__(self, pf: dict):
        self.pf = pf
        self.proc: subprocess.Popen | None = None
        self.log = None
        self.local_port = 0

    def __enter__(self) -> "PortForward":
        kubectl = shutil.which("kubectl")
        if not kubectl:
            raise CaptureError("--port-forward needs kubectl on PATH")
        self.local_port = free_port()
        self.log = tempfile.TemporaryFile()
        cmd = [kubectl, "--context", self.pf["context"], "-n", self.pf["namespace"],
               "port-forward", f"svc/{self.pf['service']}",
               f"{self.local_port}:{self.pf['port']}"]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=self.log,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + PF_READY_TIMEOUT
            while True:
                if self.proc.poll() is not None:
                    raise CaptureError(f"kubectl port-forward exited ({self.proc.returncode}) "
                                       f"before it was ready: {self._tail()}")
                if port_open(self.local_port):
                    return self
                if time.monotonic() > deadline:
                    raise CaptureError(f"kubectl port-forward not ready after "
                                       f"{PF_READY_TIMEOUT:g}s: {self._tail()}")
                time.sleep(0.2)
        except BaseException:
            self.close()
            raise

    def _tail(self) -> str:
        try:
            self.log.seek(0)
            return self.log.read()[-500:].decode(errors="replace").strip() or "(no output)"
        except (OSError, ValueError):
            return "(no output)"

    def close(self) -> None:
        p = self.proc
        if p is not None and p.poll() is None:
            # kubectl may be a wrapper with children: signal the whole group.
            for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
                try:
                    os.killpg(p.pid, sig)
                except (ProcessLookupError, PermissionError):
                    try:
                        p.send_signal(sig)
                    except ProcessLookupError:
                        pass
                try:
                    p.wait(timeout=wait)
                    break
                except subprocess.TimeoutExpired:
                    continue
        if p is not None:
            try:
                os.killpg(p.pid, signal.SIGKILL)   # stragglers in the group
            except (ProcessLookupError, PermissionError, OSError):
                pass
            if p.poll() is None:
                p.kill()
                p.wait()
        if self.log is not None:
            self.log.close()
            self.log = None

    def __exit__(self, *exc) -> None:
        self.close()


# --- OTel mapping -----------------------------------------------------------

def _attr(k: str, v: str) -> dict:
    return {"key": k, "value": {"stringValue": str(v)}}


def otel_lines(capture_id: str, name: str, promql: str, doc: dict):
    """One OTLP-JSON MetricsData line per series: {"resourceMetrics": [one record]}."""
    for s in (doc.get("data") or {}).get("result") or []:
        labels = dict(s.get("metric") or {})
        attrs = [_attr(k, labels[k]) for k in sorted(labels)]
        attrs += [_attr("investigation.capture_id", capture_id),
                  _attr("investigation.query_name", name)]
        points = []
        for ts, v in s.get("values") or []:
            try:
                x = float(v)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(x):       # JSON has no NaN; the raw file keeps it
                continue
            points.append({"timeUnixNano": str(int(round(float(ts) * 1000)) * 1_000_000),
                           "asDouble": x})
        rm = {"resource": {"attributes": attrs},
              "scopeMetrics": [{
                  "scope": {"name": "investigation.capture_prom",
                            "version": str(CAPTURE_VERSION)},
                  "metrics": [{"name": name, "description": promql,
                               "gauge": {"dataPoints": points}}]}]}
        yield json.dumps({"resourceMetrics": [rm]}, separators=(",", ":"))


# --- main -------------------------------------------------------------------

def size_guard() -> int:
    v = os.environ.get("INVESTIGATION_SIZE_GUARD_BYTES")
    if v is None or v == "":
        return SIZE_GUARD_BYTES
    try:
        n = int(v)
    except ValueError:
        raise CaptureError("INVESTIGATION_SIZE_GUARD_BYTES must be an integer", 2)
    return n


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="capture_prom.py",
        description="Capture a frozen Prometheus query_range snapshot into a study.")
    ap.add_argument("--study", required=True, help="investigations/<slug>")
    ap.add_argument("--capture-id", required=True)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="Prometheus/Thanos base URL (no credentials in it)")
    src.add_argument("--port-forward", metavar="CTX/NS/SVC[:PORT]",
                     help=f"kubectl port-forward to svc (remote port default {DEFAULT_REMOTE_PORT})")
    ap.add_argument("--token-env", metavar="VAR",
                    help="name of the env var holding a bearer token (the value is never logged)")
    ap.add_argument("--allow-insecure-http", action="store_true",
                    help="allow --url to use plain http to a non-loopback host "
                         "(the bearer token would be sent in cleartext)")
    ap.add_argument("--start", required=True, help="RFC 3339, e.g. 2026-11-05T10:00:00Z")
    ap.add_argument("--end", required=True, help="RFC 3339")
    ap.add_argument("--step", default="30s", help="query resolution (default 30s)")
    ap.add_argument("--queries", default=str(DEFAULT_QUERIES),
                    help="YAML {name: promql} (default: assets/promql/starter.yaml)")
    ap.add_argument("--extra-queries", help="YAML {name: promql}, merged last")
    return ap


def run(args) -> int:
    study = Path(args.study).expanduser().resolve()
    if not study.is_dir():
        raise CaptureError(f"--study: not a directory: {args.study}", 2)
    if not NAME_RE.match(args.capture_id):
        raise CaptureError(f"--capture-id must match {NAME_RE.pattern}", 2)

    start = parse_time(args.start, "--start")
    end = parse_time(args.end, "--end")
    if end <= start:
        raise CaptureError("--end must be after --start", 2)
    step = parse_step(args.step)
    check_step(start, end, step)
    guard = size_guard()

    token = None
    if args.token_env:
        token = os.environ.get(args.token_env)
        if not token:
            raise CaptureError(f"--token-env: env var {args.token_env} is unset or empty", 2)
        _SECRETS.append(token)

    if args.url:
        base, host = parse_url(args.url, allow_insecure_http=args.allow_insecure_http)
        source = {"kind": "url", "host": host}
        cluster = host
        pf = None
    else:
        pf = parse_port_forward(args.port_forward)
        source = {"kind": "port-forward",
                  "host": f"{pf['service']}.{pf['namespace']}.svc:{pf['port']}", **pf}
        cluster = pf["context"]
    if token:
        source["auth"] = "bearer"
        source["token_env"] = args.token_env

    # Queries: starter (or --queries), then repo promql_extra, then --extra-queries.
    queries = load_queries(Path(args.queries).expanduser(), "--queries")
    sources = [str(Path(args.queries).expanduser())]
    top = _config.repo_toplevel(study)
    if top is not None:
        try:
            extra = _config.load_repo_config(top).get("promql_extra")
        except _config.ConfigError as e:
            raise CaptureError(f"repo config: {e}", 2)
        if extra:
            queries.update(check_query_map(extra, "repo config promql_extra"))
            sources.append("repo-config:promql_extra")
    if args.extra_queries:
        queries.update(load_queries(Path(args.extra_queries).expanduser(), "--extra-queries"))
        sources.append(str(Path(args.extra_queries).expanduser()))
    if not queries:
        raise CaptureError("no queries to run", 2)

    cap_dir = study / "telemetry" / args.capture_id
    if cap_dir.exists() and any(cap_dir.iterdir()):
        raise CaptureError(f"{cap_dir} already exists; captures are frozen, pick a new --capture-id", 2)
    repo = _config.repo_name(top) if top is not None else "_no-repo"
    runs_dest = _config.runs_dir(repo, study.name) / f"telemetry-{args.capture_id}"
    if runs_dest.exists() and any(runs_dest.iterdir()):
        raise CaptureError(f"{runs_dest} already exists; pick a new --capture-id", 2)

    window = {"start": rfc3339(start), "end": rfc3339(end), "step": fmt_step(step)}
    print(f"capture_prom: {len(queries)} queries, {window['start']} -> {window['end']} "
          f"step {window['step']}, source {source['kind']} {source['host']}")

    pf_ctx = PortForward(pf) if pf else None
    results: dict[str, tuple] = {}
    try:
        if pf_ctx:
            pf_ctx.__enter__()
            base = f"http://127.0.0.1:{pf_ctx.local_port}"
        for name, promql in queries.items():
            results[name] = query_range(base, token, promql, start, end, step)
    finally:
        if pf_ctx:
            pf_ctx.close()

    failed = {n: r[2] for n, r in results.items() if r[2] is not None}
    if len(failed) == len(results):
        for n, e in failed.items():
            err(f"{n}: {e}")
        raise CaptureError(f"all {len(results)} queries failed; nothing written")

    # Write the snapshot.
    prom_dir = cap_dir / "prom"
    prom_dir.mkdir(parents=True, exist_ok=True)
    empty, summary_q = [], {}
    with open(cap_dir / "metrics.jsonl", "w", encoding="utf-8") as mj:
        for name, (raw, doc, error) in results.items():
            if raw is not None:
                # Byte-for-byte for success bodies. An error body can echo the
                # request (Authorization included); only the secret is masked.
                if error is not None:
                    raw = redact_bytes(raw)
                (prom_dir / f"{name}.json").write_bytes(raw)
            entry = {"query": queries[name]}
            if error is None:
                entry.update(summarize_query_range(doc))
                if not (doc.get("data") or {}).get("result"):
                    empty.append(name)
                for line in otel_lines(args.capture_id, name, queries[name], doc):
                    mj.write(line + "\n")
            else:
                entry.update(summarize_query_range({}))
                entry["error"] = error
            summary_q[name] = entry

    summary = {"summary_version": SUMMARY_VERSION, "capture_id": args.capture_id,
               "window": window, "queries": summary_q}
    (cap_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    bulk = sorted(p for p in prom_dir.glob("*.json")) + [cap_dir / "metrics.jsonl"]
    total = sum(p.stat().st_size for p in bulk) + (cap_dir / "summary.json").stat().st_size
    moved = {}
    if total > guard:
        runs_dest.mkdir(parents=True, exist_ok=True)
        for p in bulk:
            rel = p.relative_to(cap_dir).as_posix()
            digest = sha256_file(p)
            dest = runs_dest / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(dest))
            moved[rel] = {"sha256": digest, "path": str(dest)}
        try:
            prom_dir.rmdir()
        except OSError:
            pass

    files = {p.relative_to(cap_dir).as_posix(): sha256_file(p)
             for p in sorted(cap_dir.rglob("*")) if p.is_file() and p.name != "capture.json"}
    capture = {
        "capture_version": CAPTURE_VERSION,
        "source": source,
        "cluster": cluster,
        "window": window,
        "queries": queries,
        "query_sources": sources,
        "empty_queries": empty,
        "failed_queries": failed,
        "captured_at": rfc3339(dt.datetime.now(dt.timezone.utc).replace(microsecond=0)),
        "files": files,
        "moved": moved,
    }
    (cap_dir / "capture.json").write_text(json.dumps(capture, indent=2) + "\n")

    ok = len(results) - len(failed)
    print(f"capture_prom: wrote {cap_dir} ({ok} ok, {len(empty)} empty, {len(failed)} failed)")
    for n in empty:
        print(f"  empty: {n}")
    for n, e in failed.items():
        print(redact(f"  failed: {n}: {e}"))
    if moved:
        print(f"capture_prom: {total} bytes > size guard {guard}; prom/ and metrics.jsonl "
              f"moved to {runs_dest} (sha256 in capture.json 'moved')")
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except CaptureError as e:
        err(str(e))
        return e.code
    except KeyboardInterrupt:
        err("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
