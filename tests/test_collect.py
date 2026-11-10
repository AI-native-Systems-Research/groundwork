"""Tests for scripts/collect.py — the Nous v2 work-dir collector.

Fixture layout (all under tests/fixtures/):
  workdir-example/         realistic pass-shape work dir: the verbatim
                           docs/example-campaign/ from agentic-strategy-evolution
                           origin/nous-v2-fixes, plus a synthesized state.json /
                           report.md / principles.json (the example ships no
                           state/report/principles)
  workdir-running/         phase EXECUTE, no stop_reason, no answer/final_check
  workdir-answer-invalid/  final_check says answer_valid false, C1 named
  workdir-no-baseline/     stop_reason no_baseline, no baseline.json
  workdir-v1/              v0.5.0 state.json (last_entered_phase)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills/investigation/scripts" / "collect.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXAMPLE = FIXTURES / "workdir-example"

sys.path.insert(0, str(REPO / "skills/investigation/scripts"))
import collect  # noqa: E402


def run_collect(work_dir, study=None):
    cmd = [sys.executable, str(SCRIPT), str(work_dir)]
    if study is not None:
        cmd += ["--study", str(study)]
    return subprocess.run(cmd, capture_output=True, text=True)


def snapshot(root: Path) -> dict:
    """mtime_ns + size of every file under root, for the read-only proof."""
    out = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            p = Path(dirpath) / name
            st = p.stat()
            out[str(p)] = (st.st_mtime_ns, st.st_size)
    return out


class ExampleFixtureTest(unittest.TestCase):
    """The realistic pass-shape fixture, collected as a subprocess."""

    def test_collects_usable_bundle(self):
        r = run_collect(EXAMPLE)
        self.assertEqual(r.returncode, 0, r.stderr)
        bundle = json.loads(r.stdout)

        self.assertEqual(bundle["run_id"], "toy-cache-p99")
        self.assertEqual(bundle["phase"], "REPORT")
        self.assertEqual(bundle["stop_reason"], "answered")
        self.assertEqual(bundle["work_dir"], str(EXAMPLE))
        self.assertEqual(bundle["objective"], {"metric": "p99_ms", "direction": "decrease"})
        self.assertTrue(bundle["usable"])
        self.assertIsNone(bundle["reason"])

        guards = {g["label"]: g for g in bundle["guards"]}
        for label in ("finished", "objective declared", "answer checked", "baseline", "read-only"):
            self.assertTrue(guards[label]["ok"], guards[label])

        # final_check carried verbatim
        with open(EXAMPLE / "final_check.json") as f:
            self.assertEqual(bundle["final_check"], json.load(f))

        # claims from the work dir's own answer.json, rebased and verified
        claims = bundle["claims_draft"]
        self.assertTrue(claims)
        by_id = {c["id"]: c for c in claims}
        self.assertEqual(set(by_id), {"C1", "C2", "C0"})
        for c in claims:
            self.assertTrue(c["verified"], c)
            self.assertTrue(Path(c["evidence"]).is_absolute(), c)
            self.assertTrue(Path(c["evidence"]).is_file(), c)
        self.assertEqual(by_id["C1"]["metric"], "p99_ms")
        self.assertEqual(by_id["C1"]["value"], 41.8)

        # answer summary is the first ~200 chars of answer.json's answer
        with open(EXAMPLE / "answer.json") as f:
            answer_text = json.load(f)["answer"]
        self.assertEqual(bundle["answer_summary"], answer_text[:200])

        # latest findings from runs/iter-1/findings.json
        self.assertEqual(bundle["latest_findings"]["best"], {"metric": "p99_ms", "value": 41.8})
        self.assertIs(bundle["latest_findings"]["question_answered"], True)

        # baseline block
        self.assertTrue(bundle["baseline"]["present"])
        self.assertEqual(bundle["baseline"]["task_type"], "optimize")

        # artifacts: absolute paths of files actually found
        for key in ("state", "ledger", "answer", "final_check", "baseline",
                    "report", "campaign_yaml"):
            self.assertTrue(Path(bundle["artifacts"][key]).is_file(), key)

    def test_answer_summary_is_truncated(self):
        r = run_collect(EXAMPLE)
        bundle = json.loads(r.stdout)
        self.assertLessEqual(len(bundle["answer_summary"]), 200)
        self.assertGreater(len(bundle["answer_summary"]), 100)


class SyntheticFixtureTests(unittest.TestCase):
    def test_running_campaign(self):
        r = run_collect(FIXTURES / "workdir-running")
        self.assertEqual(r.returncode, 0, r.stderr)
        bundle = json.loads(r.stdout)
        self.assertFalse(bundle["usable"])
        self.assertIn("still running", bundle["reason"])
        guards = {g["label"]: g for g in bundle["guards"]}
        self.assertFalse(guards["finished"]["ok"])
        self.assertIn("EXECUTE", guards["finished"]["detail"])
        self.assertIsNone(bundle["final_check"])
        self.assertEqual(bundle["claims_draft"], [])

    def test_answer_invalid(self):
        r = run_collect(FIXTURES / "workdir-answer-invalid")
        self.assertEqual(r.returncode, 0, r.stderr)
        bundle = json.loads(r.stdout)
        self.assertFalse(bundle["usable"])
        guards = {g["label"]: g for g in bundle["guards"]}
        self.assertFalse(guards["answer checked"]["ok"])
        by_id = {c["id"]: c for c in bundle["claims_draft"]}
        self.assertFalse(by_id["C1"]["verified"])
        self.assertTrue(by_id["C2"]["verified"])

    def test_no_baseline(self):
        r = run_collect(FIXTURES / "workdir-no-baseline")
        self.assertEqual(r.returncode, 0, r.stderr)
        bundle = json.loads(r.stdout)
        guards = {g["label"]: g for g in bundle["guards"]}
        self.assertFalse(guards["baseline"]["ok"])
        self.assertIn("no_baseline", guards["baseline"]["detail"])
        self.assertFalse(bundle["baseline"]["present"])
        self.assertEqual(bundle["baseline"]["stop_reason"], "no_baseline")
        self.assertEqual(bundle["baseline"]["baseline_status"], "partial")

    def test_v1_work_dir_rejected(self):
        r = run_collect(FIXTURES / "workdir-v1")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "")
        self.assertIn("pre-v2", r.stderr)
        self.assertIn("v0.5.0", r.stderr)

    def test_not_a_directory(self):
        r = run_collect(FIXTURES / "no-such-dir")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a directory", r.stderr)

    def test_unreadable_ledger_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "state.json").write_text('{"phase": "REPORT", "iteration": 1, "stop_reason": "answered"}')
            (d / "ledger.json").write_text("{not json")
            r = run_collect(d)
            self.assertEqual(r.returncode, 1)
            self.assertIn("ledger.json", r.stderr)


class ReadOnlyAndStudyTests(unittest.TestCase):
    def test_collect_is_read_only(self):
        """Nothing in the work dir changes, with or without --study."""
        before = snapshot(EXAMPLE)
        with tempfile.TemporaryDirectory() as td:
            r1 = run_collect(EXAMPLE)                      # stdout mode
            r2 = run_collect(EXAMPLE, study=td)            # --study mode
            self.assertEqual(r1.returncode, 0, r1.stderr)
            self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual(snapshot(EXAMPLE), before)

    def test_study_writes_only_collection_json(self):
        """--study writes exactly one new file and touches nothing else."""
        with tempfile.TemporaryDirectory() as td:
            study = Path(td) / "study"
            shutil.copytree(EXAMPLE, study)  # a study dir that already has content
            before = snapshot(study)
            r = run_collect(EXAMPLE, study=study)
            self.assertEqual(r.returncode, 0, r.stderr)
            after = snapshot(study)
            new = set(after) - set(before)
            changed = {p for p in set(before) & set(after) if before[p] != after[p]}
            self.assertEqual(new, {str(study / "collection.json")})
            self.assertEqual(changed, set())
            with open(study / "collection.json") as f:
                collection = json.load(f)
            self.assertEqual(collection["collection_version"], 2)
            self.assertTrue(collection["nous"]["usable"])

    def test_study_merge_preserves_other_keys(self):
        """--study merges under "nous" and keeps a pre-existing "spotlights" key."""
        with tempfile.TemporaryDirectory() as td:
            study = Path(td)
            spot = {"run_id": "run-x", "claims_draft": [{"id": "c1"}]}
            (study / "collection.json").write_text(json.dumps(
                {"collection_version": 2, "spotlights": spot,
                 "nous": {"stale": True}}))
            r = run_collect(EXAMPLE, study=study)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(study / "collection.json") as f:
                collection = json.load(f)
            self.assertEqual(collection["spotlights"], spot)
            self.assertEqual(collection["collection_version"], 2)
            self.assertNotIn("stale", collection["nous"])
            self.assertEqual(collection["nous"]["run_id"], "toy-cache-p99")
            self.assertEqual(r.stdout, "")

    def test_study_refuses_non_object_collection(self):
        with tempfile.TemporaryDirectory() as td:
            study = Path(td)
            (study / "collection.json").write_text("[1, 2]")
            r = run_collect(EXAMPLE, study=study)
            self.assertEqual(r.returncode, 1)
            self.assertEqual((study / "collection.json").read_text(), "[1, 2]")

    def test_runs_dir_resolves_relative_work_dir(self):
        with tempfile.TemporaryDirectory() as td:
            runs = Path(td) / "runs"
            shutil.copytree(EXAMPLE, runs / "repo" / "slug" / "nous" / "w")
            cmd = [sys.executable, str(SCRIPT), "repo/slug/nous/w", "--runs-dir", str(runs)]
            r = subprocess.run(cmd, capture_output=True, text=True, cwd=td)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(r.stdout)["run_id"], "toy-cache-p99")


class HelperTests(unittest.TestCase):
    """Unit tests for the pure helpers, by import."""

    def test_load_json(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self.assertEqual(collect.load_json(d / "missing.json"), (None, None))
            (d / "bad.json").write_text("{nope")
            data, err = collect.load_json(d / "bad.json")
            self.assertIsNone(data)
            self.assertIn("is not valid JSON", err)
            (d / "good.json").write_text('{"a": 1}')
            self.assertEqual(collect.load_json(d / "good.json"), ({"a": 1}, None))

    def test_load_yaml(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self.assertEqual(collect.load_yaml(d / "missing.yaml"), (None, None))
            (d / "bad.yaml").write_text("foo: [unclosed")
            data, err = collect.load_yaml(d / "bad.yaml")
            self.assertIsNone(data)
            self.assertIn("is not valid YAML", err)

    def test_rebase_evidence(self):
        wd = Path("/abs/work")
        self.assertEqual(collect.rebase_evidence(wd, "nous_evidence/x.json"),
                         "/abs/work/nous_evidence/x.json")
        self.assertEqual(collect.rebase_evidence(wd, "/elsewhere/x.json"),
                         "/elsewhere/x.json")
        self.assertEqual(collect.rebase_evidence(wd, "a/b.json"), "/abs/work/a/b.json")
        rebased = collect.rebase_evidence(wd, {"path": "a.json", "key": "/x", "value": 1})
        self.assertEqual(rebased, {"path": "/abs/work/a.json", "key": "/x", "value": 1})
        self.assertIsNone(collect.rebase_evidence(wd, None))

    def test_claim_verified(self):
        errors = ["claim C1: evidence path does not exist: x.json",
                  "evidence value mismatch at y.json#/hit_rate: expected 0.7, got 0.78"]
        self.assertFalse(collect.claim_verified({"id": "C1", "metric": "p99_ms"}, errors))
        self.assertFalse(collect.claim_verified({"id": "C9", "metric": "hit_rate"}, errors))
        self.assertTrue(collect.claim_verified({"id": "C2", "metric": "other"}, errors))
        self.assertTrue(collect.claim_verified({"id": "C1", "metric": "p99_ms"}, []))
        self.assertTrue(collect.claim_verified({"id": "", "metric": ""}, errors))

    def test_draft_claims(self):
        wd = Path("/abs/work")
        answer = {"claims": [
            {"id": "C1", "metric": "p99_ms", "value": 1, "condition": "c",
             "evidence": "evidence/a.json"},
            {"id": "C2", "metric": "hit_rate", "value": 2, "condition": "c2",
             "evidence": {"path": "evidence/b.json", "key": "/v", "value": 2},
             "evidence_detail": {"path": "evidence/b.json", "key": "/v", "value": 2}},
            "not-a-claim",
        ]}
        out = collect.draft_claims(wd, answer, ["claim C2: bad"])
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["evidence"], "/abs/work/evidence/a.json")
        self.assertTrue(out[0]["verified"])
        self.assertEqual(out[1]["evidence"], "/abs/work/evidence/b.json")
        self.assertFalse(out[1]["verified"])
        self.assertEqual(out[1]["evidence_detail"]["path"], "/abs/work/evidence/b.json")
        self.assertEqual(collect.draft_claims(wd, None, []), [])
        self.assertEqual(collect.draft_claims(wd, {"claims": "nope"}, []), [])

    def test_latest_iter_dir(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self.assertIsNone(collect.latest_iter_dir(d))
            (d / "runs" / "iter-1").mkdir(parents=True)
            (d / "runs" / "iter-3").mkdir(parents=True)
            (d / "runs" / "not-iter").mkdir(parents=True)
            (d / "runs" / "iter-2.txt").write_text("x")
            self.assertEqual(collect.latest_iter_dir(d), d / "runs" / "iter-3")

    def test_build_bundle_v1_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "state.json").write_text('{"last_entered_phase": "EXECUTE", "iteration": 3}')
            with self.assertRaises(collect.CouldNotRun) as cm:
                collect.build_bundle(d)
            self.assertIn("pre-v2", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
