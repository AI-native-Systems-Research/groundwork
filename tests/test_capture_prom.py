"""Tests for skills/investigation/scripts/capture_prom.py.

A stdlib http.server in a thread plays Prometheus: it answers
/api/v1/query_range with recorded fixtures under tests/fixtures/prom/,
chosen by the PromQL text. Nothing here talks to a real cluster.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills/investigation/scripts"
SCRIPT = SCRIPTS / "capture_prom.py"
FIX = Path(__file__).resolve().parent / "fixtures" / "prom"
QUERIES = FIX / "queries.yaml"
TOKEN = "s3cr3t-T0KEN-do-not-leak-7f3a9c"

sys.path.insert(0, str(SCRIPTS))
import capture_prom  # noqa: E402
import _telemetry  # noqa: E402

START, END = "2025-11-05T10:00:00Z", "2025-11-05T10:01:30Z"


def fixture_for(promql: str) -> tuple[int, bytes]:
    if "does_not_exist" in promql:
        return 200, (FIX / "query_range-empty.json").read_bytes()
    if "syntax_error" in promql:
        return 400, (FIX / "query_range-error.json").read_bytes()
    return 200, (FIX / "query_range-vllm_running.json").read_bytes()


def make_handler(expect_token=None, seen=None):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            if self.path != "/api/v1/query_range":
                self.send_error(404)
                return
            n = int(self.headers.get("Content-Length") or 0)
            form = urllib.parse.parse_qs(self.rfile.read(n).decode())
            if seen is not None:
                seen.append({"auth": self.headers.get("Authorization"), "form": form})
            if expect_token and self.headers.get("Authorization") != f"Bearer {expect_token}":
                code, body = 401, b'{"status":"error","errorType":"auth","error":"unauthorized"}'
            else:
                code, body = fixture_for(form["query"][0])
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return H


class FakeProm:
    def __init__(self, **kw):
        self.seen = []
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(seen=self.seen, **kw))
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="capture-prom-test-")).resolve()
        self.study = self.tmp / "investigations" / "demo-study"
        self.study.mkdir(parents=True)
        self.runs = self.tmp / "runs"
        self.env = {**os.environ,
                    "INVESTIGATION_RUNS": str(self.runs),
                    "INVESTIGATION_CONFIG_HOME": str(self.tmp / "cfg"),
                    "GIT_CEILING_DIRECTORIES": str(self.tmp)}
        self.env.pop("INVESTIGATION_SIZE_GUARD_BYTES", None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args, env=None, cid="c1"):
        cmd = [sys.executable, str(SCRIPT), "--study", str(self.study),
               "--capture-id", cid, "--start", START, "--end", END,
               "--queries", str(QUERIES), *args]
        return subprocess.run(cmd, capture_output=True, text=True, env=env or self.env,
                              timeout=60)

    def cap_dir(self, cid="c1"):
        return self.study / "telemetry" / cid


class TestCaptureUrl(Base):
    def test_capture_writes_snapshot(self):
        with FakeProm() as p:
            r = self.run_cli("--url", p.url + "/", "--step", "30s")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = self.cap_dir()
        # Raw bytes are preserved exactly.
        self.assertEqual((d / "prom/running.json").read_bytes(),
                         (FIX / "query_range-vllm_running.json").read_bytes())
        cap = json.loads((d / "capture.json").read_text())
        self.assertEqual(cap["capture_version"], _telemetry.CAPTURE_VERSION)
        self.assertEqual(cap["source"]["kind"], "url")
        self.assertEqual(cap["source"]["host"], p.url.split("//", 1)[1])
        self.assertEqual(cap["window"], {"start": START, "end": END, "step": "30s"})
        self.assertEqual(set(cap["queries"]), {"running", "nothing"})
        self.assertEqual(cap["moved"], {})
        for k in ("cluster", "captured_at", "empty_queries", "files"):
            self.assertIn(k, cap)
        # files covers everything except capture.json, with correct hashes.
        on_disk = {q.relative_to(d).as_posix() for q in d.rglob("*")
                   if q.is_file() and q.name != "capture.json"}
        self.assertEqual(set(cap["files"]), on_disk)
        for rel, sha in cap["files"].items():
            self.assertEqual(_telemetry.sha256_file(d / rel), sha)
        # summary.json matches summarize_query_range.
        summ = json.loads((d / "summary.json").read_text())
        self.assertEqual(summ["summary_version"], _telemetry.SUMMARY_VERSION)
        self.assertEqual(summ["capture_id"], "c1")
        self.assertEqual(summ["window"], cap["window"])
        raw = json.loads((FIX / "query_range-vllm_running.json").read_text())
        want = {"query": cap["queries"]["running"], **_telemetry.summarize_query_range(raw)}
        self.assertEqual(summ["queries"]["running"], want)
        self.assertEqual(summ["queries"]["running"]["overall"]["n_points"], 7)  # NaN dropped
        # Request parameters.
        form = p.seen[0]["form"]
        self.assertEqual(form["step"], ["30"])
        self.assertIsNone(p.seen[0]["auth"])

    def test_metrics_jsonl_otel_shape(self):
        with FakeProm() as p:
            r = self.run_cli("--url", p.url)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = (self.cap_dir() / "metrics.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 2)       # 2 series of `running`; `nothing` is empty
        rec = json.loads(lines[1])
        rm = rec["resourceMetrics"]
        self.assertEqual(len(rm), 1)
        attrs = {a["key"]: a["value"]["stringValue"] for a in rm[0]["resource"]["attributes"]}
        self.assertEqual(attrs["pod"], "vllm-decode-1")
        self.assertEqual(attrs["investigation.query_name"], "running")
        self.assertEqual(attrs["investigation.capture_id"], "c1")
        m = rm[0]["scopeMetrics"][0]["metrics"][0]
        self.assertEqual(m["name"], "running")
        pts = m["gauge"]["dataPoints"]
        self.assertEqual(len(pts), 3)          # the NaN sample is skipped
        self.assertEqual(pts[0], {"timeUnixNano": "1762336800000000000", "asDouble": 1.0})

    def test_empty_query_recorded_not_failure(self):
        with FakeProm() as p:
            r = self.run_cli("--url", p.url)
        self.assertEqual(r.returncode, 0, r.stderr)
        cap = json.loads((self.cap_dir() / "capture.json").read_text())
        self.assertEqual(cap["empty_queries"], ["nothing"])
        self.assertTrue((self.cap_dir() / "prom/nothing.json").is_file())
        s = json.loads((self.cap_dir() / "summary.json").read_text())["queries"]["nothing"]
        self.assertEqual(s["overall"]["n_series"], 0)
        self.assertIsNone(s["overall"]["mean"])
        self.assertIn("empty: nothing", r.stdout)

    def test_prometheus_error_recorded_and_total_failure_exits_nonzero(self):
        q = self.tmp / "q.yaml"
        q.write_text("bad: syntax_error(\n")
        with FakeProm() as p:
            r = self.run_cli("--url", p.url, "--extra-queries", str(q))
        self.assertEqual(r.returncode, 0, r.stderr)
        cap = json.loads((self.cap_dir() / "capture.json").read_text())
        self.assertIn("bad", cap["failed_queries"])
        self.assertIn("bad_data", cap["failed_queries"]["bad"])
        self.assertTrue((self.cap_dir() / "prom/bad.json").is_file())  # error body kept
        self.assertIn("error", json.loads((self.cap_dir() / "summary.json").read_text())
                      ["queries"]["bad"])
        # Only the bad query: total failure.
        only = self.tmp / "only.yaml"
        only.write_text("bad: syntax_error(\n")
        with FakeProm() as p:
            r = subprocess.run([sys.executable, str(SCRIPT), "--study", str(self.study),
                                "--capture-id", "c2", "--start", START, "--end", END,
                                "--queries", str(only), "--url", p.url],
                               capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn("all 1 queries failed", r.stderr)
        self.assertFalse(self.cap_dir("c2").exists())

    def test_unreachable_is_total_failure(self):
        port = capture_prom.free_port()
        r = self.run_cli("--url", f"http://127.0.0.1:{port}")
        self.assertEqual(r.returncode, 1)
        self.assertFalse(self.cap_dir().exists())

    def test_refuses_existing_capture(self):
        with FakeProm() as p:
            self.assertEqual(self.run_cli("--url", p.url).returncode, 0)
            r = self.run_cli("--url", p.url)
        self.assertEqual(r.returncode, 2)
        self.assertIn("frozen", r.stderr)

    def test_url_with_credentials_rejected(self):
        r = self.run_cli("--url", "http://user:pw@127.0.0.1:9/")
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("pw@", r.stderr)

    def test_plain_http_non_loopback_rejected(self):
        # The gate is unconditional (token present or not) and fires before any
        # query, so nothing is written to the study.
        for host in ("prom.example.com", "10.0.0.5"):
            env = {**self.env, "PROM_TOKEN": TOKEN}
            r = self.run_cli("--url", f"http://{host}:9090", "--token-env", "PROM_TOKEN",
                             env=env)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn("plain http to non-loopback host", r.stderr)
            self.assertIn("--allow-insecure-http", r.stderr)
            self.assertNotIn(TOKEN, r.stderr)
            self.assertFalse(self.cap_dir().exists())

    def test_plain_http_non_loopback_allowed_with_flag(self):
        # --allow-insecure-http acknowledges the cleartext hop: the URL is
        # accepted and the (unreachable) remote fails as a normal query error,
        # never as the cleartext refusal. INVESTIGATION_PROM_TIMEOUT bounds the
        # attempt so a network that silently blackholes the host (no local
        # proxy answering 502) still fails fast.
        env = {**self.env, "PROM_TOKEN": TOKEN, "INVESTIGATION_PROM_TIMEOUT": "2"}
        r = self.run_cli("--url", "http://10.0.0.5:9090", "--allow-insecure-http",
                         "--token-env", "PROM_TOKEN", env=env)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertNotIn("cleartext", r.stderr)
        self.assertNotIn("non-loopback", r.stderr)
        self.assertIn("all 2 queries failed", r.stderr)
        self.assertFalse(self.cap_dir().exists())

    def test_plain_http_loopback_and_https_still_allowed(self):
        # Loopback plain http (the stub server's own shape) and https to any
        # host never trip the gate.
        with FakeProm() as p:
            r = self.run_cli("--url", p.url)          # http://127.0.0.1:<port>
        self.assertEqual(r.returncode, 0, r.stderr)
        # https to a real host is never a cleartext refusal; whatever happens
        # on the network (refused, proxied 502, or blackholed — the timeout
        # bounds the attempt), the only exit-2 here would be the gate.
        env = {**self.env, "INVESTIGATION_PROM_TIMEOUT": "2"}
        r = self.run_cli("--url", "https://prom.example.com:9090", env=env, cid="c2")
        self.assertNotEqual(r.returncode, 2, r.stderr)
        self.assertNotIn("cleartext", r.stderr)
        self.assertTrue(capture_prom.is_loopback("localhost"))
        self.assertTrue(capture_prom.is_loopback("127.1.2.3"))
        self.assertFalse(capture_prom.is_loopback("10.0.0.5"))


class TestStep(Base):
    def test_step_limit_errors_with_suggestion(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--study", str(self.study),
                            "--capture-id", "c1", "--start", "2025-11-01T00:00:00Z",
                            "--end", "2025-11-08T00:00:00Z", "--step", "30s",
                            "--url", "http://127.0.0.1:9"],
                           capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("Suggested step: --step 1m", r.stderr)
        self.assertIn("never clamped", r.stderr)
        self.assertFalse((self.study / "telemetry").exists())

    def test_limit_boundary_and_helpers(self):
        s = capture_prom.parse_time("2025-11-01T00:00:00Z", "--start")
        ok = capture_prom.parse_time("2025-11-04T19:40:00Z", "--end")   # 330000 s
        capture_prom.check_step(s, ok, 30)                               # exactly 11000
        over = capture_prom.parse_time("2025-11-04T19:40:30Z", "--end")
        with self.assertRaises(capture_prom.CaptureError):
            capture_prom.check_step(s, over, 30)
        self.assertEqual(capture_prom.parse_step("1m30s"), 90)
        self.assertEqual(capture_prom.parse_step("500ms"), 0.5)
        self.assertEqual(capture_prom.parse_step("15"), 15)
        with self.assertRaises(capture_prom.CaptureError):
            capture_prom.parse_step("30x")
        self.assertEqual(capture_prom.fmt_step(300), "5m")
        with self.assertRaises(capture_prom.CaptureError):
            capture_prom.parse_time("2025-11-01T00:00:00", "--start")   # no tz

    def test_port_forward_spec(self):
        self.assertEqual(capture_prom.parse_port_forward("kind-a/monitoring/prometheus"),
                         {"context": "kind-a", "namespace": "monitoring",
                          "service": "prometheus", "port": 9090})
        self.assertEqual(
            capture_prom.parse_port_forward("arn:aws:eks:us-east-1:1:cluster/x/ns/svc:8080"),
            {"context": "arn:aws:eks:us-east-1:1:cluster/x", "namespace": "ns",
             "service": "svc", "port": 8080})
        with self.assertRaises(capture_prom.CaptureError):
            capture_prom.parse_port_forward("ns/svc")


class TestParsers(unittest.TestCase):
    """In-process tests for the parse helpers' rejection branches (the CLI
    tests in TestStep only drive the happy path and the two big failures)."""

    def err(self, fn, *args, **kw):
        with self.assertRaises(capture_prom.CaptureError) as cm:
            fn(*args, **kw)
        return str(cm.exception)

    def test_parse_time_rejects_garbage_and_missing_tz(self):
        self.assertIn("not an RFC 3339 time", self.err(capture_prom.parse_time,
                                                       "not-a-time", "--start"))
        self.assertIn("needs a timezone", self.err(capture_prom.parse_time,
                                                   "2025-11-01T00:00:00", "--start"))
        # 'z' lowercase is accepted and normalized to UTC.
        self.assertEqual(capture_prom.parse_time("2025-11-01T00:00:00z", "--start"),
                         capture_prom.parse_time("2025-11-01T00:00:00Z", "--start"))

    def test_parse_step_rejects_gaps_and_non_positive(self):
        # A gap between durations is not a duration ("1mX30s").
        self.assertIn("not a duration", self.err(capture_prom.parse_step, "1mX30s"))
        self.assertIn("not a duration", self.err(capture_prom.parse_step, "-5s"))
        self.assertIn("must be positive", self.err(capture_prom.parse_step, "-5"))
        self.assertIn("must be positive", self.err(capture_prom.parse_step, "0"))
        self.assertIn("must be positive", self.err(capture_prom.parse_step, "inf"))

    def test_suggest_step_beyond_the_ladder_rounds_to_days(self):
        # A window longer than ladder-max 86400s * 11000 needs a step the
        # ladder cannot supply: the suggestion becomes a whole number of days.
        dur = 86400 * capture_prom.MAX_POINTS_PER_SERIES * 3 + 3600
        self.assertEqual(capture_prom.suggest_step(dur), 86400 * 4)

    def test_parse_url_rejects_scheme_query_fragment_and_brackets_ipv6(self):
        self.assertIn("http(s) URL", self.err(capture_prom.parse_url, "ftp://h"))
        self.assertIn("http(s) URL", self.err(capture_prom.parse_url, "http://"))
        self.assertIn("query string or fragment", self.err(capture_prom.parse_url,
                                                           "http://h/?x=1"))
        self.assertIn("query string or fragment", self.err(capture_prom.parse_url,
                                                           "http://h/#frag"))
        # An explicit port is included verbatim; without one, a literal-IPv6
        # host keeps its brackets (the bracketing branch).
        base, host = capture_prom.parse_url("http://[::1]/prom")
        self.assertEqual((base, host), ("http://[::1]/prom", "[::1]"))

    def test_parse_port_forward_rejects_bad_ports(self):
        for spec in ("ctx/ns/svc:0", "ctx/ns/svc:99999", "ctx/ns/svc:x", "ctx/ns/:9090",
                     "ctx/ns/svc:"):
            with self.subTest(spec=spec):
                self.assertIn("bad service port",
                              self.err(capture_prom.parse_port_forward, spec))

    def test_size_guard_env_validation(self):
        self.assertEqual(capture_prom.size_guard(), _telemetry.SIZE_GUARD_BYTES)
        with mock.patch.dict(os.environ, {"INVESTIGATION_SIZE_GUARD_BYTES": "100"}):
            self.assertEqual(capture_prom.size_guard(), 100)
        with mock.patch.dict(os.environ, {"INVESTIGATION_SIZE_GUARD_BYTES": ""}):
            self.assertEqual(capture_prom.size_guard(), _telemetry.SIZE_GUARD_BYTES)
        with mock.patch.dict(os.environ, {"INVESTIGATION_SIZE_GUARD_BYTES": "ten"}):
            self.assertIn("must be an integer", self.err(capture_prom.size_guard))


class TestQueryMap(unittest.TestCase):
    """load_queries / check_query_map validation against tempdir files."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="capture-prom-qmap-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, name, text):
        p = self.tmp / name
        p.write_text(textwrap.dedent(text))
        return p

    def err(self, fn, *args):
        with self.assertRaises(capture_prom.CaptureError) as cm:
            fn(*args)
        return str(cm.exception)

    def test_missing_file(self):
        self.assertIn("no such file",
                      self.err(capture_prom.load_queries, self.tmp / "nope.yaml", "--queries"))

    def test_unparseable_yaml(self):
        p = self.write("bad.yaml", "a: [unclosed\n")
        self.assertIn("--queries", self.err(capture_prom.load_queries, p, "--queries"))

    def test_empty_file_gives_no_queries(self):
        p = self.write("empty.yaml", "")
        self.assertEqual(capture_prom.load_queries(p, "--queries"), {})

    def test_check_query_map_rejections(self):
        e = self.err(capture_prom.check_query_map, ["not", "a", "map"], "where")
        self.assertIn("flat", e)
        e = self.err(capture_prom.check_query_map, {"bad name!": "up"}, "where")
        self.assertIn("must match", e)
        e = self.err(capture_prom.check_query_map, {"q": 5}, "where")
        self.assertIn("non-empty PromQL", e)
        e = self.err(capture_prom.check_query_map, {"q": "  "}, "where")
        self.assertIn("non-empty PromQL", e)
        # Values are stripped on the way through.
        self.assertEqual(capture_prom.check_query_map({"q": " up(1) \n"}, "where"),
                         {"q": "up(1)"})


class TestHttpResponseClassification(Base):
    """query_range's four malformed-response branches: the fake server answers
    the query named `bad` with each malformed shape and every other query with
    the recorded success fixture, so the capture is written (a partial failure,
    not a total one) and the classified error lands in capture.json."""

    # mode -> (status, body): served to the query named `bad`.
    MODES = {
        # HTTPError with a non-JSON body.
        "http_nonjson": (500, b"<html>gateway error</html>"),
        # HTTPError whose body IS JSON but says nothing about status (a proxy's
        # 502 payload): falls through to the plain "HTTP <code>" error.
        "http_json_no_status": (502, b'{"upstream": "unavailable"}'),
        # 200 whose body is not JSON at all.
        "ok_nonjson": (200, b"<html>login page</html>"),
        # 200 with JSON that is not an object.
        "ok_nonobject": (200, b'["a", "list"]'),
        # 200 with a JSON object that says status: error.
        "ok_error": (200, b'{"status":"error","errorType":"execution",'
                           b'"error":"query timed out"}'),
    }

    BAD = "does_not_exist"       # the fixture handler answers this with an empty result

    def capture(self, mode):
        q = self.tmp / "q.yaml"
        q.write_text(f"good: vllm_running\n{self.BAD}: up(1)\n")
        status, body = self.MODES[mode]
        handler = self.handler_for(status, body)
        with FakeServer(handler) as p:
            r = self.run_cli("--url", p.url, "--queries", str(q))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"failed: {self.BAD}", r.stdout)
        return json.loads((self.cap_dir() / "capture.json").read_text())

    @staticmethod
    def handler_for(status, body):
        ok = (FIX / "query_range-vllm_running.json").read_bytes()

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                form = urllib.parse.parse_qs(self.rfile.read(n).decode())
                if "vllm_running" in form["query"][0]:
                    code, out = 200, ok
                else:
                    code, out = status, body
                self.send_response(code)
                self.send_header("Content-Type", "text/html" if b"<" in out
                                 else "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
        return H

    def test_http_error_with_non_json_body(self):
        cap = self.capture("http_nonjson")
        self.assertIn("HTTP 500", cap["failed_queries"][self.BAD])
        # No raw body to keep for this query: only the good one is under prom/.
        self.assertEqual([p.name for p in (self.cap_dir() / "prom").iterdir()],
                         ["good.json"])

    def test_http_error_with_json_body_that_is_not_an_error(self):
        cap = self.capture("http_json_no_status")
        self.assertIn("HTTP 502", cap["failed_queries"][self.BAD])
        # No raw body is kept: the JSON body did not say status=error.
        self.assertEqual([p.name for p in (self.cap_dir() / "prom").iterdir()],
                         ["good.json"])

    def test_ok_non_json_body(self):
        cap = self.capture("ok_nonjson")
        self.assertIn("not JSON", cap["failed_queries"][self.BAD])
        self.assertIn("auth proxy", cap["failed_queries"][self.BAD])

    def test_ok_non_object_json(self):
        cap = self.capture("ok_nonobject")
        self.assertIn("not a JSON object", cap["failed_queries"][self.BAD])

    def test_ok_error_status_body(self):
        cap = self.capture("ok_error")
        self.assertIn("execution", cap["failed_queries"][self.BAD])
        self.assertIn("timed out", cap["failed_queries"][self.BAD])
        # The body IS valid JSON: it is kept byte-for-byte as evidence.
        self.assertEqual((self.cap_dir() / "prom" / f"{self.BAD}.json").read_bytes(),
                         self.MODES["ok_error"][1])

    def test_error_message_is_redacted(self):
        # A token-shaped substring inside the server's error text must be
        # redacted before it reaches capture.json (query_range redacts the
        # classification, run() redacts the printed line).
        token = "leaky-TOKEN-value"
        body = (b'{"status":"error","errorType":"exec","error":"boom '
                + token.encode() + b'"}')
        q = self.tmp / "q.yaml"
        q.write_text(f"good: vllm_running\n{self.BAD}: up(1)\n")
        handler = self.handler_for(200, body)
        env = {**self.env, "PROM_TOKEN": token}
        with FakeServer(handler) as p:
            r = self.run_cli("--url", p.url, "--queries", str(q),
                             "--token-env", "PROM_TOKEN", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        cap = json.loads((self.cap_dir() / "capture.json").read_text())
        self.assertNotIn(token, cap["failed_queries"][self.BAD])
        self.assertIn("***", cap["failed_queries"][self.BAD])
        self.assertNotIn(token, r.stdout + r.stderr)
        self.assertNotIn(token, (self.cap_dir() / "capture.json").read_text())


class TestRunGuards(Base):
    """run()'s argument and environment guards, via the CLI (exit code 2)."""

    def test_study_must_be_a_directory(self):
        r = self.run_cli("--url", "http://127.0.0.1:9",
                         "--study", str(self.tmp / "nope"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("not a directory", r.stderr)

    def test_capture_id_must_match_name_re(self):
        r = self.run_cli("--url", "http://127.0.0.1:9", cid="bad id!")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--capture-id", r.stderr)

    def test_end_must_be_after_start(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--study", str(self.study),
                            "--capture-id", "c1", "--start", END, "--end", START,
                            "--url", "http://127.0.0.1:9"],
                           capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("after --start", r.stderr)

    def test_no_queries_to_run(self):
        q = self.tmp / "empty.yaml"
        q.write_text("")                     # load_queries: {} -> "no queries"
        with FakeProm() as p:
            r = self.run_cli("--url", p.url, "--queries", str(q))
        self.assertEqual(r.returncode, 2)
        self.assertIn("no queries to run", r.stderr)

    def test_existing_runs_destination_refused(self):
        # The moved-bulk destination under the runs dir is as frozen as the
        # capture dir itself: a pre-existing one refuses before any query.
        dest = self.runs / "_no-repo" / "demo-study" / "telemetry-c1"
        dest.mkdir(parents=True)
        dest.joinpath("sentinel").write_text("x")
        with FakeProm() as p:
            r = self.run_cli("--url", p.url)
        self.assertEqual(r.returncode, 2)
        self.assertIn("already exists", r.stderr)
        self.assertIn("pick a new --capture-id", r.stderr)

    def test_unparseable_repo_config(self):
        top = self.tmp / "repo"
        self.study = top / "investigations" / "s1"
        self.study.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(top)], check=True)
        (top / "investigations/config.yaml").write_text("tracking: [unclosed\n")
        env = {**self.env, "GIT_CEILING_DIRECTORIES": ""}
        with FakeProm() as p:
            r = self.run_cli("--url", p.url, env=env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("repo config", r.stderr)

    def test_bad_size_guard_env(self):
        env = {**self.env, "INVESTIGATION_SIZE_GUARD_BYTES": "ten"}
        with FakeProm() as p:
            r = self.run_cli("--url", p.url, env=env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("must be an integer", r.stderr)


class FakeServer:
    """A bare ThreadingHTTPServer with a caller-supplied handler class."""

    def __init__(self, handler):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()


class TestOtelLines(unittest.TestCase):
    def test_non_float_and_nan_values_are_skipped(self):
        doc = {"data": {"result": [
            {"metric": {"pod": "a"}, "values": [[1, "3"], [2, "NaN"], [3, "N/A"],
                                                [4, None], [5, "5"]]}]}}
        lines = list(capture_prom.otel_lines("cap-1", "q", "up(1)", doc))
        self.assertEqual(len(lines), 1)
        pts = json.loads(lines[0])["resourceMetrics"][0]["scopeMetrics"][0] \
            ["metrics"][0]["gauge"]["dataPoints"]
        self.assertEqual([p["asDouble"] for p in pts], [3.0, 5.0])
        attrs = {a["key"]: a["value"]["stringValue"]
                 for a in json.loads(lines[0])["resourceMetrics"][0]["resource"]["attributes"]}
        self.assertEqual(attrs["investigation.capture_id"], "cap-1")
        self.assertEqual(attrs["investigation.query_name"], "q")


class FakeProc:
    """A PortForward.close() target that behaves like a wedged or dead
    subprocess.Popen without being one. `term_works` decides whether a
    delivered signal ends the wait (a real kubectl usually exits on SIGTERM)."""

    def __init__(self, pid, *, term_works=True):
        self.pid = pid
        self.term_works = term_works
        self._alive = True
        self.signals = []

    def poll(self):
        return None if self._alive else 0

    def wait(self, timeout=None):
        if self._alive:
            raise subprocess.TimeoutExpired(cmd="kubectl", timeout=timeout)
        return 0

    def send_signal(self, sig):
        self.signals.append(sig)
        if self.term_works:
            self._alive = False

    def kill(self):
        self.signals.append("kill")
        self._alive = False


class TestPortForwardTeardown(unittest.TestCase):
    def make_pf(self, proc):
        pf = capture_prom.PortForward({"context": "c", "namespace": "n",
                                       "service": "s", "port": 1})
        pf.proc = proc
        pf.log = tempfile.TemporaryFile()
        self.addCleanup(pf.log.close)
        return pf

    def test_missing_kubectl_is_a_clean_error(self):
        pf = capture_prom.PortForward({"context": "c", "namespace": "n",
                                       "service": "s", "port": 1})
        with mock.patch.object(capture_prom.shutil, "which", return_value=None):
            with self.assertRaises(capture_prom.CaptureError) as cm:
                pf.__enter__()
        self.assertIn("kubectl on PATH", str(cm.exception))

    def test_close_falls_back_to_send_signal_when_the_group_is_gone(self):
        # killpg raises ProcessLookupError (the process group leader died and
        # the pid lingers): close() signals the process itself instead.
        proc = FakeProc(pid=99999999)
        pf = self.make_pf(proc)

        def killpg(pid, sig):
            raise ProcessLookupError()

        with mock.patch.object(capture_prom.os, "killpg", side_effect=killpg):
            pf.close()
        self.assertEqual(proc.signals, [signal.SIGTERM])  # reached the process

    def test_close_escalates_to_sigkill_when_term_times_out(self):
        # The process ignores every delivered signal and never exits: the
        # signal loop's SIGTERM wait times out, the loop continues to SIGKILL,
        # and the straggler path still tries killpg before p.kill().
        proc = FakeProc(pid=99999999, term_works=False)
        pf = self.make_pf(proc)
        seen = []

        def killpg(pid, sig):
            seen.append(sig)
            if sig == signal.SIGKILL:
                proc._alive = False     # the SIGKILL lands
            return None

        with mock.patch.object(capture_prom.os, "killpg", side_effect=killpg):
            pf.close()
        # SIGTERM and SIGKILL both went to the group; p.kill() was not needed.
        self.assertEqual(seen, [signal.SIGTERM, signal.SIGKILL, signal.SIGKILL])
        self.assertNotIn("kill", proc.signals)

    def test_close_kills_the_process_directly_as_a_last_resort(self):
        # Both killpg calls raise (gone entirely): the direct p.kill() runs.
        proc = FakeProc(pid=99999999, term_works=False)
        pf = self.make_pf(proc)

        def killpg(pid, sig):
            raise ProcessLookupError()

        with mock.patch.object(capture_prom.os, "killpg", side_effect=killpg):
            pf.close()
        # SIGTERM via send_signal (loop), SIGKILL via send_signal (loop), then
        # the straggler killpg failed again -> p.kill() + p.wait().
        self.assertEqual(proc.signals, [signal.SIGTERM, signal.SIGKILL, "kill"])

    def test_close_survives_a_vanished_process(self):
        # The process disappears between killpg failing and send_signal:
        # close() must swallow the ProcessLookupError and keep tearing down.

        class Vanished(FakeProc):
            def send_signal(self, sig):
                self.signals.append(sig)
                raise ProcessLookupError()

            def kill(self):
                self.signals.append("kill")
                self._alive = False

        proc = Vanished(pid=99999999, term_works=False)
        pf = self.make_pf(proc)

        def killpg(pid, sig):
            raise ProcessLookupError()

        with mock.patch.object(capture_prom.os, "killpg", side_effect=killpg):
            pf.close()
        # The signal loop's p.wait() timed out after both signals vanished,
        # then the straggler path fell through to p.kill().
        self.assertEqual(proc.signals, [signal.SIGTERM, signal.SIGKILL, "kill"])

    def test_exit_runs_the_same_teardown_as_close(self):
        # PortForward is a context manager: __exit__ must tear the forward
        # down even when no exception is propagating.
        proc = FakeProc(pid=99999999)
        pf = self.make_pf(proc)
        with mock.patch.object(capture_prom.os, "killpg") as killpg:
            killpg.return_value = None
            proc._alive = False        # already exited: killpg does the work
            pf.__exit__(None, None, None)
        self.assertEqual(proc.signals, [])        # nothing signalled directly
        self.assertEqual(killpg.call_count, 1)    # only the straggler pass
        self.assertIsNone(pf.log)                 # log handle was closed

    def test_tail_reports_no_output_on_unreadable_log(self):
        pf = self.make_pf(FakeProc(pid=99999999))

        class Broken:
            def seek(self, *a):
                raise OSError()

            def read(self, *a):
                raise OSError()

        pf.log = Broken()
        self.assertEqual(pf._tail(), "(no output)")


class TestTokenLeak(Base):
    def test_token_never_on_disk_or_output(self):
        env = {**self.env, "PROM_TOKEN": TOKEN, "INVESTIGATION_SIZE_GUARD_BYTES": "1"}
        q = self.tmp / "q.yaml"
        q.write_text("bad: syntax_error(\n")
        with FakeProm(expect_token=TOKEN) as p:
            r = self.run_cli("--url", p.url, "--token-env", "PROM_TOKEN",
                             "--extra-queries", str(q), env=env)
            # Also: a wrong token (server 401 on every query) and an unreachable host.
            r2 = subprocess.run([sys.executable, str(SCRIPT), "--study", str(self.study),
                                 "--capture-id", "c2", "--start", START, "--end", END,
                                 "--queries", str(QUERIES), "--url", p.url,
                                 "--token-env", "PROM_TOKEN"],
                                capture_output=True, text=True, timeout=60,
                                env={**env, "PROM_TOKEN": TOKEN + "-wrong"})
        r3 = self.run_cli("--url", f"http://127.0.0.1:{capture_prom.free_port()}",
                          "--token-env", "PROM_TOKEN", env=env, cid="c3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r2.returncode, 1)
        self.assertEqual(r3.returncode, 1)
        # The token was actually sent.
        self.assertIn(f"Bearer {TOKEN}", [s["auth"] for s in p.seen])
        cap = json.loads((self.cap_dir() / "capture.json").read_text())
        self.assertTrue(cap["moved"])          # the size guard path is covered too
        self.assertEqual(cap["source"].get("token_env"), "PROM_TOKEN")
        for out in (r.stdout, r.stderr, r2.stdout, r2.stderr, r3.stdout, r3.stderr):
            self.assertNotIn(TOKEN, out)
        n = 0
        for root in (self.study, self.runs):
            for f in root.rglob("*"):
                if f.is_file():
                    n += 1
                    self.assertNotIn(TOKEN.encode(), f.read_bytes(), f)
        self.assertGreater(n, 3)

    def test_missing_token_env_fails_cleanly(self):
        env = {k: v for k, v in self.env.items() if k != "PROM_TOKEN"}
        r = self.run_cli("--url", "http://127.0.0.1:9", "--token-env", "PROM_TOKEN", env=env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("PROM_TOKEN", r.stderr)


class TestSizeGuard(Base):
    def test_over_guard_moves_bulk_to_runs_dir(self):
        env = {**self.env, "INVESTIGATION_SIZE_GUARD_BYTES": "100"}
        with FakeProm() as p:
            r = self.run_cli("--url", p.url, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        d = self.cap_dir()
        self.assertEqual(sorted(x.name for x in d.iterdir()), ["capture.json", "summary.json"])
        cap = json.loads((d / "capture.json").read_text())
        self.assertEqual(set(cap["files"]), {"summary.json"})
        self.assertEqual(set(cap["moved"]),
                         {"prom/running.json", "prom/nothing.json", "metrics.jsonl"})
        dest = self.runs / "_no-repo" / "demo-study" / "telemetry-c1"
        for rel, info in cap["moved"].items():
            self.assertEqual(Path(info["path"]), dest / rel)
            self.assertEqual(_telemetry.sha256_file(Path(info["path"])), info["sha256"])
        # prom_path follows the move.
        self.assertEqual(_telemetry.prom_path(d, "running"), dest / "prom/running.json")
        self.assertEqual(Path(_telemetry.prom_path(d, "running")).read_bytes(),
                         (FIX / "query_range-vllm_running.json").read_bytes())

    def test_under_guard_stays_in_study(self):
        with FakeProm() as p:
            r = self.run_cli("--url", p.url)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.runs.exists())
        self.assertTrue((self.cap_dir() / "metrics.jsonl").is_file())


class TestRepoConfig(Base):
    def test_promql_extra_merged_and_runs_dir_uses_repo(self):
        top = self.tmp / "repo"
        self.study = top / "investigations" / "s1"
        self.study.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(top)], check=True)
        (top / "investigations/config.yaml").write_text(
            "tracking: fork\npromql_extra:\n  extra_q: sum(vllm:num_requests_waiting)\n")
        env = {**self.env, "GIT_CEILING_DIRECTORIES": "",
               "INVESTIGATION_SIZE_GUARD_BYTES": "10"}
        with FakeProm() as p:
            r = self.run_cli("--url", p.url, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        cap = json.loads((self.cap_dir() / "capture.json").read_text())
        self.assertIn("extra_q", cap["queries"])
        self.assertIn("repo-config:promql_extra", cap["query_sources"])
        self.assertTrue(str(next(iter(cap["moved"].values()))["path"]).startswith(
            str(self.runs / "repo" / "s1" / "telemetry-c1")))

    def test_starter_yaml_is_flat_mapping(self):
        import yaml
        d = yaml.safe_load(capture_prom.DEFAULT_QUERIES.read_text())
        self.assertGreater(len(d), 20)
        self.assertEqual(capture_prom.check_query_map(d, "starter"), {k: v.strip() for k, v in d.items()})
        self.assertNotIn("by (decision)", " ".join(d.values()))


STUB_SERVER = textwrap.dedent('''\
    #!{py}
    # Fake kubectl: `kubectl --context C -n NS port-forward svc/S LOCAL:REMOTE`.
    import os, sys, time
    a = sys.argv[1:]
    open(os.environ["STUB_PIDFILE"], "w").write(str(os.getpid()))
    open(os.environ["STUB_ARGSFILE"], "w").write("\\n".join(a))
    mode = os.environ.get("STUB_MODE", "serve")
    if mode == "sleep":
        while True:
            time.sleep(1)
    if mode == "exit":
        print("error: services \\"nope\\" not found", file=sys.stderr)
        sys.exit(1)
    local = int(a[-1].split(":")[0])
    sys.path.insert(0, os.environ["STUB_TESTDIR"])
    from test_capture_prom import make_handler
    from http.server import HTTPServer
    time.sleep(0.5)    # not ready immediately: exercises the polling
    print(f"Forwarding from 127.0.0.1:{{local}} -> {{a[-1].split(':')[1]}}", flush=True)
    HTTPServer(("127.0.0.1", local), make_handler()).serve_forever()
''')


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie (exited, not reaped) counts as gone.
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(st.stdout.strip()) and not st.stdout.strip().startswith("Z")


class TestPortForward(Base):
    def setUp(self):
        super().setUp()
        bindir = self.tmp / "bin"
        bindir.mkdir()
        k = bindir / "kubectl"
        k.write_text(STUB_SERVER.format(py=sys.executable))
        k.chmod(0o755)
        self.pidfile = self.tmp / "kubectl.pid"
        self.argsfile = self.tmp / "kubectl.args"
        self.env.update(PATH=f"{bindir}{os.pathsep}{self.env['PATH']}",
                        STUB_PIDFILE=str(self.pidfile), STUB_ARGSFILE=str(self.argsfile),
                        STUB_TESTDIR=str(Path(__file__).resolve().parent),
                        PYTHONPATH="")

    def assert_gone(self):
        pid = int(self.pidfile.read_text())
        for _ in range(20):
            if not pid_alive(pid):
                return
            time.sleep(0.1)
        os.kill(pid, 9)
        self.fail(f"kubectl stub pid {pid} still running after capture_prom exited")

    def test_port_forward_capture_and_teardown(self):
        r = self.run_cli("--port-forward", "kind-test/monitoring/prometheus-k8s:9091")
        self.assertEqual(r.returncode, 0, r.stderr)
        args = self.argsfile.read_text().splitlines()
        self.assertEqual(args[:5], ["--context", "kind-test", "-n", "monitoring", "port-forward"])
        self.assertEqual(args[5], "svc/prometheus-k8s")
        self.assertTrue(args[6].endswith(":9091"))
        cap = json.loads((self.cap_dir() / "capture.json").read_text())
        self.assertEqual(cap["source"]["kind"], "port-forward")
        self.assertEqual(cap["source"]["context"], "kind-test")
        self.assertEqual(cap["source"]["namespace"], "monitoring")
        self.assertEqual(cap["source"]["service"], "prometheus-k8s")
        self.assertNotIn(args[6].split(":")[0], cap["source"]["host"])   # no local port
        self.assertEqual(cap["cluster"], "kind-test")
        self.assertTrue((self.cap_dir() / "prom/running.json").is_file())
        self.assert_gone()

    def test_never_ready_times_out_and_kills(self):
        env = {**self.env, "STUB_MODE": "sleep", "INVESTIGATION_PF_TIMEOUT": "1.5"}
        r = self.run_cli("--port-forward", "ctx/ns/svc", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not ready", r.stderr)
        self.assert_gone()
        self.assertFalse(self.cap_dir().exists())

    def test_kubectl_exits_early(self):
        env = {**self.env, "STUB_MODE": "exit"}
        r = self.run_cli("--port-forward", "ctx/ns/nope", env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not found", r.stderr)


if __name__ == "__main__":
    unittest.main()
