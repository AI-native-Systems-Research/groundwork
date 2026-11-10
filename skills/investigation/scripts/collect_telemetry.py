#!/usr/bin/env python3
"""Collect one frozen telemetry capture into summary.json + collection.json.

Reads <study>/telemetry/<capture-id>/ (written by capture_prom.py; format
pinned in _telemetry.py) and:

  1. verifies every file sha256 recorded in capture.json "files" (in the
     study) and "moved" (relocated to the runs dir by the size guard), and
     reports missing files and mismatches;
  2. reduces each query's raw query_range response (found through
     _telemetry.prom_path, so moved prom/ files work) to per-series and
     overall {mean, p50, max, min, n_points, n_series}, written to
     telemetry/<id>/summary.json;
  3. drafts measured claims (overall mean / p50 / max per query) whose
     condition names the cluster, the absolute window and step, and the
     query name + PromQL, and whose evidence is telemetry/<id>/summary.json.
     The value is the number exactly as json.dumps wrote it into
     summary.json, so the validator's value-in-evidence check passes;
  4. merges the result under collection.json["telemetry"][<capture-id>],
     keeping other captures and other top-level keys (see _collection.py).

It does NOT detect anomalies and does NOT write 01_signals.json: anomaly
detection is spotlights' job (F1). An empty query (no series, or only
NaN/Inf samples) gives no claim, only a note.

summary.json is DERIVED, so it is not part of the pass/fail integrity check:
the prom files are the source of truth. Its capture-time sha256 (from
capture.json "files") is compared with the file found on disk and reported
under integrity.summary_json, along with whether this collector rewrote it.
capture.json is never modified (it is the frozen capture record), so after a
rewrite its summary.json hash is stale by design; integrity.summary_json and
a note say so. Because the regeneration walks queries in capture.json order
with capture_prom.py's entry shape, an untouched capture is not rewritten.

Failed queries (capture.json "failed_queries") keep capture_prom.py's
summary entry (null stats + "error") and give a note, never a claim. Extra
capture.json keys (query_sources, source.auth / token_env, ...) are ignored.

A claim is verified:true only when the capture's integrity check passed for
the files that claim depends on (the query's prom file and summary.json is
regenerated from it) and its value string is literally in summary.json;
otherwise verified:false, matching collect.py's draft-claim convention.

Usage: python3 scripts/collect_telemetry.py --study DIR --capture-id ID

Exit 0 = collected (integrity problems are carried in the section, with
usable:false, and echoed on stderr). Exit 1 = could not run: the study or
capture dir is missing, capture.json is unreadable, or collection.json
exists but isn't a JSON object.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True  # an install inside a target repo must not dirty it (D4)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _collection  # noqa: E402
from _telemetry import SUMMARY_VERSION, prom_path, sha256_file, summarize_query_range  # noqa: E402

CLAIM_STATS = ("mean", "p50", "max")
SUMMARY_FILE = "summary.json"


class CouldNotRun(Exception):
    pass


def load_capture(capture_dir: Path) -> dict:
    path = capture_dir / "capture.json"
    try:
        cap = json.loads(path.read_text())
    except FileNotFoundError as e:
        raise CouldNotRun(f"no capture.json in {capture_dir}") from e
    except (OSError, ValueError) as e:
        raise CouldNotRun(f"{path} is unreadable: {e}") from e
    if not isinstance(cap, dict):
        raise CouldNotRun(f"{path} is not a JSON object")
    return cap


def verify_integrity(capture_dir: Path, cap: dict) -> dict:
    """Check every recorded sha256. summary.json is reported separately as
    'regenerated' by the caller, never as a mismatch here, because this
    collector rewrites it deterministically from the prom files."""
    checked, mismatches, missing = [], [], []

    def check(rel: str, path: Path, expected) -> None:
        if not path.is_file():
            missing.append({"file": rel, "path": str(path)})
            return
        actual = sha256_file(path)
        checked.append(rel)
        if actual != expected:
            mismatches.append({"file": rel, "path": str(path),
                               "expected": expected, "actual": actual})

    files = cap.get("files") or {}
    moved = cap.get("moved") or {}
    for rel, expected in sorted(files.items()):
        if rel == SUMMARY_FILE:
            continue
        local = capture_dir / rel
        if not local.is_file() and rel in moved:
            continue  # recorded both ways; the moved entry is checked below
        check(rel, local, expected)
    for rel, info in sorted(moved.items()):
        if not isinstance(info, dict) or "path" not in info:
            missing.append({"file": rel, "path": None})
            continue
        check(rel, Path(info["path"]).expanduser(), info.get("sha256"))
    return {"ok": not mismatches and not missing, "checked": checked,
            "mismatches": mismatches, "missing": missing}


def build_summary(capture_dir: Path, cap: dict, capture_id: str, notes: list) -> dict:
    """Re-derive summary.json from the prom files, in capture.json query
    order and with capture_prom.py's entry shape ({query, overall, series}
    plus "error" for a failed query), so an untouched capture reproduces the
    capture-time summary.json byte for byte."""
    queries = cap.get("queries") or {}
    failed = cap.get("failed_queries") or {}
    out = {}
    for name, promql in queries.items():
        entry = {"query": promql}
        error = None
        if name in failed:
            error = str(failed[name])
            notes.append(f"query {name}: failed at capture ({error}); no claim")
        else:
            path = prom_path(capture_dir, name)
            try:
                resp = json.loads(Path(path).read_text())
            except FileNotFoundError:
                error = f"prom/{name}.json not found ({path})"
            except (OSError, ValueError) as e:
                error = f"prom/{name}.json unreadable: {e}"
            if error is not None:
                notes.append(f"query {name}: {error}; no claim")
        if error is None:
            entry.update(summarize_query_range(resp if isinstance(resp, dict) else {}))
        else:
            entry.update(summarize_query_range({}))
            entry["error"] = error
        out[name] = entry
    return {"summary_version": SUMMARY_VERSION, "capture_id": capture_id,
            "window": cap.get("window"), "queries": out}


def describe_cluster(cap: dict) -> str:
    cluster = cap.get("cluster")
    if cluster:
        return f"cluster {cluster}"
    src = cap.get("source") or {}
    host = src.get("host") if isinstance(src, dict) else None
    return f"cluster unnamed (source {host})" if host else "cluster unnamed"


def draft_claims(capture_id: str, cap: dict, summary: dict, summary_text: str,
                 integrity: dict, notes: list) -> list:
    window = cap.get("window") or {}
    where = (f"{describe_cluster(cap)}, window {window.get('start')} – {window.get('end')} "
             f"(step {window.get('step')})")
    bad = {m["file"] for m in integrity["mismatches"]} | {m["file"] for m in integrity["missing"]}
    empty = set(cap.get("empty_queries") or [])
    evidence = f"telemetry/{capture_id}/{SUMMARY_FILE}"
    claims = []
    for name, q in summary["queries"].items():
        ov = q["overall"]
        if "error" in q:
            continue  # already noted in build_summary
        if ov.get("n_points", 0) == 0:
            why = "recorded as empty at capture" if name in empty else "no finite samples"
            notes.append(f"query {name}: empty ({why}); no claim")
            continue
        prom_ok = f"prom/{name}.json" not in bad
        for stat in CLAIM_STATS:
            value = ov.get(stat)
            if value is None:
                continue
            literal = json.dumps(value)
            claims.append({
                "id": f"tm-{capture_id}-{name}-{stat}",
                "metric": f"{name} {stat}",
                "value": value,
                "condition": (f"{where}, query {name}: {q['query']}; overall {stat} "
                              f"over {ov['n_series']} series, {ov['n_points']} points"),
                "evidence": evidence,
                "verified": prom_ok and literal in summary_text,
            })
    return claims


def collect(study: Path, capture_id: str) -> dict:
    capture_dir = study / "telemetry" / capture_id
    if not capture_dir.is_dir():
        raise CouldNotRun(f"{capture_dir} is not a directory")
    cap = load_capture(capture_dir)
    notes: list[str] = []

    integrity = verify_integrity(capture_dir, cap)
    for m in integrity["mismatches"]:
        notes.append(f"sha256 mismatch: {m['file']} ({m['path']}) expected {m['expected']}, "
                     f"got {m['actual']}")
    for m in integrity["missing"]:
        notes.append(f"recorded file missing: {m['file']} ({m['path']})")

    summary = build_summary(capture_dir, cap, capture_id, notes)
    summary_text = json.dumps(summary, indent=2) + "\n"
    summary_path = capture_dir / SUMMARY_FILE
    recorded = (cap.get("files") or {}).get(SUMMARY_FILE)
    before = sha256_file(summary_path) if summary_path.is_file() else None
    regenerated = summary_path.read_text() != summary_text if before else True
    if regenerated:
        summary_path.write_text(summary_text)
    after = sha256_file(summary_path)
    summary_check = {"recorded_sha256": recorded, "sha256_before": before,
                     "sha256_after": after, "rewritten": regenerated}
    if recorded and before and before != recorded:
        notes.append(f"summary.json had been edited since capture (sha256 {before}, "
                     f"capture.json records {recorded}); regenerated from the prom files")
    elif regenerated and recorded:
        notes.append("summary.json regenerated and differs from the capture-time one "
                     f"(capture.json records {recorded}, now {after}); capture.json is "
                     "left untouched")
    integrity["summary_json"] = summary_check

    claims = draft_claims(capture_id, cap, summary, summary_text, integrity, notes)
    window = cap.get("window") or {}
    failures = []
    if not integrity["ok"]:
        failures.append(f"integrity check failed ({len(integrity['mismatches'])} mismatch(es), "
                        f"{len(integrity['missing'])} missing)")
    return {
        "capture_id": capture_id,
        "capture_dir": str(capture_dir),
        "summary": f"telemetry/{capture_id}/{SUMMARY_FILE}",
        "summary_regenerated": regenerated,
        "cluster": cap.get("cluster"),
        "source": cap.get("source"),
        "window": {"start": window.get("start"), "end": window.get("end"),
                   "step": window.get("step")},
        "queries": cap.get("queries") or {},
        "empty_queries": list(cap.get("empty_queries") or []),
        "captured_at": cap.get("captured_at"),
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "integrity": integrity,
        "usable": not failures,
        "reason": "; ".join(failures) if failures else None,
        "notes": notes,
        "claims_draft": claims,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--study", required=True, metavar="DIR")
    parser.add_argument("--capture-id", required=True, metavar="ID")
    args = parser.parse_args(argv)

    study = Path(args.study).expanduser()
    if not study.is_dir():
        print(f"--study {study} is not a directory", file=sys.stderr)
        return 1
    study = study.resolve()
    if "/" in args.capture_id or args.capture_id in ("", ".", ".."):
        print(f"bad --capture-id {args.capture_id!r}", file=sys.stderr)
        return 1
    try:
        section = collect(study, args.capture_id)
        path = _collection.merge_subsection(study, "telemetry", args.capture_id, section)
    except (CouldNotRun, _collection.CollectionError) as e:
        print(e, file=sys.stderr)
        return 1
    if not section["usable"]:
        print(f"warning: {section['reason']}", file=sys.stderr)
    print(f"{path}: telemetry.{args.capture_id} — {len(section['claims_draft'])} draft "
          f"claim(s), {len(section['notes'])} note(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
