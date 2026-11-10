"""Tests for skills/investigation/scripts/collect_telemetry.py.

Fixture tests/fixtures/telemetry/cap-a/ is a hand-built capture in the
capture_prom.py format: capture.json (real sha256s), prom/<name>.json raw
query_range responses (one with a NaN sample, one empty), metrics.jsonl.
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills/investigation/scripts"
SCRIPT = SCRIPTS / "collect_telemetry.py"
VALIDATOR = SCRIPTS / "validate_answer.py"
CAP = Path(__file__).resolve().parent / "fixtures" / "telemetry" / "cap-a"

sys.path.insert(0, str(SCRIPTS))
from _telemetry import sha256_file  # noqa: E402


def run(study, capture_id="cap-a"):
    return subprocess.run([sys.executable, str(SCRIPT), "--study", str(study),
                           "--capture-id", capture_id], capture_output=True, text=True)


class TelemetryCollectTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.study = self.root / "study"
        shutil.copytree(CAP, self.study / "telemetry" / "cap-a")
        self.cap_dir = self.study / "telemetry" / "cap-a"

    def tearDown(self):
        self._td.cleanup()

    def collection(self):
        return json.loads((self.study / "collection.json").read_text())

    def test_summary_and_claims(self):
        r = run(self.study)
        self.assertEqual(r.returncode, 0, r.stderr)
        summary = json.loads((self.cap_dir / "summary.json").read_text())
        self.assertEqual(summary["capture_id"], "cap-a")
        kv = summary["queries"]["kv_cache_usage"]
        self.assertEqual(kv["query"], "avg by (pod) (vllm:kv_cache_usage_perc)")
        self.assertEqual(kv["overall"], {"mean": 0.4, "p50": 0.375, "max": 0.75,
                                         "min": 0.125, "n_points": 5, "n_series": 2})
        self.assertEqual(len(kv["series"]), 2)
        self.assertEqual(kv["series"][1]["n_points"], 2)  # NaN dropped
        self.assertEqual(kv["series"][1]["labels"]["pod"], "vllm-1")

        col = self.collection()
        self.assertEqual(col["collection_version"], 2)
        sec = col["telemetry"]["cap-a"]
        self.assertTrue(sec["usable"], sec)
        self.assertTrue(sec["integrity"]["ok"])
        claims = sec["claims_draft"]
        names = {c["metric"] for c in claims}
        self.assertEqual(names, {"kv_cache_usage mean", "kv_cache_usage p50",
                                 "kv_cache_usage max", "num_requests_waiting mean",
                                 "num_requests_waiting p50", "num_requests_waiting max"})
        text = (self.cap_dir / "summary.json").read_text()
        for c in claims:
            self.assertTrue(c["verified"], c)
            self.assertEqual(c["evidence"], "telemetry/cap-a/summary.json")
            self.assertIn(json.dumps(c["value"]), text)
            for part in ("cluster test-cluster-a", "2025-11-05T00:00:00Z",
                         "2025-11-05T00:01:30Z", "step 30s"):
                self.assertIn(part, c["condition"])
        kvmean = next(c for c in claims if c["metric"] == "kv_cache_usage mean")
        self.assertEqual(kvmean["value"], 0.4)
        self.assertIn("kv_cache_usage: avg by (pod) (vllm:kv_cache_usage_perc)",
                      kvmean["condition"])

    def test_empty_query_note_no_claim(self):
        r = run(self.study)
        self.assertEqual(r.returncode, 0, r.stderr)
        sec = self.collection()["telemetry"]["cap-a"]
        self.assertFalse([c for c in sec["claims_draft"] if "prefix_hit_rate" in c["metric"]])
        self.assertTrue(any("prefix_hit_rate" in n and "empty" in n for n in sec["notes"]))

    def test_no_anomaly_detection_output(self):
        r = run(self.study)
        self.assertEqual(r.returncode, 0, r.stderr)
        for name in ("01_signals.json", "signals.json", "signal.json", "traces.jsonl"):
            self.assertEqual(list(self.study.rglob(name)), [], name)

    def test_claims_pass_the_validator(self):
        r = run(self.study)
        self.assertEqual(r.returncode, 0, r.stderr)
        claims = self.collection()["telemetry"]["cap-a"]["claims_draft"]
        (self.study / "final").mkdir()
        (self.study / "deliverable.md").write_text("x\n")
        answer = {"answer": "a", "deliverable": "deliverable.md", "repro": "r",
                  "claims": [{k: c[k] for k in ("id", "metric", "value", "condition",
                                                "evidence")} for c in claims]}
        ap = self.study / "final" / "answer.json"
        ap.write_text(json.dumps(answer))
        v = subprocess.run([sys.executable, str(VALIDATOR), str(ap)],
                           capture_output=True, text=True)
        self.assertEqual(v.returncode, 0, v.stdout + v.stderr)

    def test_moved_prom_files(self):
        runs = self.root / "runs"
        runs.mkdir()
        cap = json.loads((self.cap_dir / "capture.json").read_text())
        rel = "prom/kv_cache_usage.json"
        dest = runs / "kv_cache_usage.json"
        shutil.move(self.cap_dir / rel, dest)
        cap["moved"] = {rel: {"sha256": cap["files"].pop(rel), "path": str(dest)}}
        (self.cap_dir / "capture.json").write_text(json.dumps(cap))
        r = run(self.study)
        self.assertEqual(r.returncode, 0, r.stderr)
        sec = self.collection()["telemetry"]["cap-a"]
        self.assertTrue(sec["integrity"]["ok"], sec["integrity"])
        self.assertIn(rel, sec["integrity"]["checked"])
        self.assertTrue(any(c["metric"] == "kv_cache_usage mean" and c["verified"]
                            for c in sec["claims_draft"]))

    def test_sha_mismatch_reported(self):
        p = self.cap_dir / "prom" / "num_requests_waiting.json"
        doc = json.loads(p.read_text())
        doc["data"]["result"][0]["values"][0][1] = "300"
        p.write_text(json.dumps(doc))
        r = run(self.study)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("integrity check failed", r.stderr)
        sec = self.collection()["telemetry"]["cap-a"]
        self.assertFalse(sec["usable"])
        mm = sec["integrity"]["mismatches"]
        self.assertEqual([m["file"] for m in mm], ["prom/num_requests_waiting.json"])
        self.assertEqual(mm[0]["actual"], sha256_file(p))
        for c in sec["claims_draft"]:
            self.assertEqual(c["verified"], not c["metric"].startswith("num_requests_waiting"), c)

    def test_missing_moved_file_reported(self):
        cap = json.loads((self.cap_dir / "capture.json").read_text())
        cap["moved"] = {"metrics.jsonl": {"sha256": "0" * 64,
                                          "path": str(self.root / "gone.jsonl")}}
        (self.cap_dir / "capture.json").write_text(json.dumps(cap))
        r = run(self.study)
        self.assertEqual(r.returncode, 0, r.stderr)
        sec = self.collection()["telemetry"]["cap-a"]
        self.assertFalse(sec["integrity"]["ok"])
        self.assertEqual([m["file"] for m in sec["integrity"]["missing"]], ["metrics.jsonl"])

    def test_merge_keeps_other_captures_and_keys(self):
        shutil.copytree(CAP, self.study / "telemetry" / "cap-b")
        (self.study / "collection.json").write_text(json.dumps(
            {"collection_version": 2, "nous": {"run_id": "n"}, "spotlights": {"x": 1}}))
        self.assertEqual(run(self.study, "cap-a").returncode, 0)
        self.assertEqual(run(self.study, "cap-b").returncode, 0)
        col = self.collection()
        self.assertEqual(set(col["telemetry"]), {"cap-a", "cap-b"})
        self.assertEqual(col["nous"], {"run_id": "n"})
        self.assertEqual(col["spotlights"], {"x": 1})

    def test_legacy_v1_collection_kept_under_nous(self):
        legacy = {"run_id": "old", "guards": [], "usable": True, "claims_draft": []}
        (self.study / "collection.json").write_text(json.dumps(legacy))
        self.assertEqual(run(self.study).returncode, 0)
        col = self.collection()
        self.assertEqual(col["nous"], legacy)
        self.assertIn("cap-a", col["telemetry"])

    def test_missing_capture_is_exit_1(self):
        r = run(self.study, "nope")
        self.assertEqual(r.returncode, 1)
        self.assertFalse((self.study / "collection.json").exists())

    def test_untouched_capture_summary_not_rewritten(self):
        """The regenerated summary matches capture_prom's byte for byte, so an
        untouched capture keeps its recorded summary.json sha256."""
        recorded = json.loads((self.cap_dir / "capture.json").read_text())["files"]["summary.json"]
        self.assertEqual(run(self.study).returncode, 0)
        self.assertEqual(sha256_file(self.cap_dir / "summary.json"), recorded)
        sec = self.collection()["telemetry"]["cap-a"]
        self.assertFalse(sec["summary_regenerated"])
        self.assertFalse(sec["integrity"]["summary_json"]["rewritten"])
        self.assertEqual(run(self.study).returncode, 0)
        self.assertEqual(sha256_file(self.cap_dir / "summary.json"), recorded)

    def test_edited_summary_regenerated_not_a_mismatch(self):
        p = self.cap_dir / "summary.json"
        p.write_text(p.read_text().replace('"max": 0.75', '"max": 9.75'))
        before = (self.cap_dir / "capture.json").read_text()
        r = run(self.study)
        self.assertEqual(r.returncode, 0, r.stderr)
        sec = self.collection()["telemetry"]["cap-a"]
        self.assertTrue(sec["usable"], sec)        # derived file: not an integrity failure
        self.assertTrue(sec["summary_regenerated"])
        self.assertTrue(any("edited since capture" in n for n in sec["notes"]))
        self.assertNotIn("9.75", p.read_text())
        self.assertEqual((self.cap_dir / "capture.json").read_text(), before)

    def test_failed_query_note_no_claim(self):
        self.assertEqual(run(self.study).returncode, 0)
        sec = self.collection()["telemetry"]["cap-a"]
        self.assertFalse([c for c in sec["claims_draft"] if "vllm_running" in c["metric"]])
        self.assertTrue(any("vllm_running" in n and "failed" in n for n in sec["notes"]))
        summary = json.loads((self.cap_dir / "summary.json").read_text())
        self.assertIn("error", summary["queries"]["vllm_running"])

if __name__ == "__main__":
    unittest.main()
