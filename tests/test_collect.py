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
        # the value-mismatch family carries a <path>#<key> pointer, no claim
        # id: it taints the claim whose metric is the pointer's key
        self.assertFalse(collect.claim_verified({"id": "C9", "metric": "hit_rate"}, errors))
        self.assertTrue(collect.claim_verified({"id": "C2", "metric": "other"}, errors))
        self.assertTrue(collect.claim_verified({"id": "C1", "metric": "p99_ms"}, []))
        self.assertTrue(collect.claim_verified({"id": "", "metric": ""}, errors))

    def test_claim_verified_id_contamination(self):
        """'C1' is a substring of 'C12': an error naming C12 must not taint
        C1 (word-boundary id match)."""
        errors = ["claim C12: evidence path does not exist: "
                  "nous_evidence/runs/iter-1/results/R12.json (searched work dir)"]
        self.assertTrue(collect.claim_verified({"id": "C1", "metric": "p99_ms"}, errors))
        self.assertFalse(collect.claim_verified({"id": "C12", "metric": "p99_ms"}, errors))
        # same direction for a longer shared prefix: C1N names neither C1 nor C11
        errors = ["claim C1N: evidence path does not exist: x.json"]
        self.assertTrue(collect.claim_verified({"id": "C1", "metric": "m"}, errors))
        self.assertTrue(collect.claim_verified({"id": "C11", "metric": "m"}, errors))
        # an id-shaped mention without the 'claim' prefix still taints, bounded
        self.assertFalse(collect.claim_verified(
            {"id": "C3", "metric": "m"}, ["claims_needed id C3 missing"]))

    def test_claim_verified_metric_contamination(self):
        """A shared metric name is the NORM (treatment vs baseline): an error
        pinning r9.json#/p99_ms taints only the claim citing r9.json, never
        the sibling sharing the metric but citing r5.json."""
        errors = ["evidence value mismatch at nous_evidence/r9.json#/p99_ms: "
                  "expected 43.0, got 43.2"]
        c5 = {"id": "C5", "metric": "p99_ms", "evidence": "nous_evidence/r5.json"}
        c9 = {"id": "C9", "metric": "p99_ms", "evidence": "nous_evidence/r9.json"}
        self.assertTrue(collect.claim_verified(c5, errors))
        self.assertFalse(collect.claim_verified(c9, errors))
        # with evidence_detail {path,key} both must match: same file, other
        # key -> clean
        sibling = {"id": "C10", "metric": "hit_rate",
                   "evidence": "nous_evidence/r9.json",
                   "evidence_detail": {"path": "nous_evidence/r9.json",
                                       "key": "/hit_rate", "value": 0.7}}
        self.assertTrue(collect.claim_verified(sibling, errors))
        # the detail citing the SAME file+key is tainted
        same = {"id": "C11", "metric": "p95",
                "evidence": "nous_evidence/r9.json",
                "evidence_detail": {"path": "nous_evidence/r9.json",
                                    "key": "/p99_ms", "value": 43.2}}
        self.assertFalse(collect.claim_verified(same, errors))

    def test_claim_verified_error_without_id_or_metric(self):
        """The value-mismatch family can name the claim's evidence without its
        id or metric string: the pointer or the file must carry the taint."""
        errors = ["evidence value mismatch at results/R4.json#/latency: "
                  "expected 41.0, got 41.8"]
        # pointer key == the claim's metric (json-pointer key, no leading
        # slash equivalence) -> tainted even though the id never appears
        self.assertFalse(collect.claim_verified(
            {"id": "latency_claim_7", "metric": "latency"}, errors))
        # the claim's own {path,key} evidence names the pointer exactly
        self.assertFalse(collect.claim_verified(
            {"id": "latency_claim_7", "metric": "p95_latency_ms",
             "evidence": {"path": "results/R4.json", "key": "/latency"}}, errors))
        # evidence_detail naming the pointer exactly
        self.assertFalse(collect.claim_verified(
            {"id": "latency_claim_7", "metric": "p95_latency_ms",
             "evidence": "results/R4.json",
             "evidence_detail": {"path": "results/R4.json",
                                 "key": "/latency", "value": 41.0}}, errors))
        # a different claim citing a different file is untouched
        self.assertTrue(collect.claim_verified(
            {"id": "x", "metric": "p95_latency_ms",
             "evidence": "results/R9.json"}, errors))
        # id-less path-does-not-exist error: the file name carries the taint
        errors = ["evidence path does not exist: "
                  "nous_evidence/runs/iter-1/results/R1.json (searched work dir)"]
        self.assertFalse(collect.claim_verified(
            {"id": "latency_claim_7", "metric": "p95_latency_ms",
             "evidence": "nous_evidence/runs/iter-1/results/R1.json"}, errors))
        self.assertTrue(collect.claim_verified(
            {"id": "x", "metric": "p95_latency_ms",
             "evidence": "nous_evidence/runs/iter-1/results/R2.json"}, errors))

    def test_claim_verified_unattributable_error_taints_all(self):
        """An error that names no id, pointer or file blames everyone: a
        silent verified:true is the failure mode the guard exists to stop."""
        errors = ["the evidence could not be checked at all"]
        self.assertFalse(collect.claim_verified({"id": "C7", "metric": "m"}, errors))
        self.assertFalse(collect.claim_verified({"id": "", "metric": ""}, errors))

    def test_named_claim_id_requires_a_digit(self):
        """A named claim id must carry a digit (every Nous claim id does).
        The letter-only spelling captured ANY 3+ char word after 'claim':
        'claim latency:' -> 'latency', 'claim the:' -> 'the', letting the
        named-id tier decide alone on prose and skip the pointer tier."""
        self.assertEqual(collect._named_claim_id("claim C1: x.json missing"), "C1")
        self.assertEqual(collect._named_claim_id("claim C12: x.json missing"), "C12")
        self.assertEqual(collect._named_claim_id("claim C1N: x.json missing"), "C1N")
        self.assertIsNone(collect._named_claim_id("claim latency: value not found"))
        self.assertIsNone(collect._named_claim_id("claim the: value not found"))
        self.assertIsNone(collect._named_claim_id("no claim prefix here:"))

    def test_claim_verified_prose_word_after_claim_falls_through_to_pointer(self):
        """Regression (bead 7369261c-a59): 'claim latency: r5.json#p99_ms
        value mismatch' used to capture the bogus named id 'latency', and
        that tier decides alone — C5 came back verified=True even though the
        pointer tier taints it via r5.json. The named id must be real first."""
        errors = ["claim latency: r5.json#p99_ms value mismatch"]
        c5 = {"id": "C5", "metric": "p99_ms", "evidence": "nous_evidence/r5.json"}
        self.assertFalse(collect.claim_verified(c5, errors))
        # a claim citing another file is untouched by the pointer
        c9 = {"id": "C9", "metric": "p99_ms", "evidence": "nous_evidence/r9.json"}
        self.assertTrue(collect.claim_verified(c9, errors))

    def test_attributable_ignores_bare_decimal_literals(self):
        """The file tier needs a real data extension: the bare 'word.ext'
        shape also matched decimals ('3.14', '99.9', '2.0'), which made an
        unattributable error look attributable and let every claim verify."""
        for literal in ("3.14", "99.9", "2.0", "0.5", "41.8"):
            self.assertFalse(collect._attributable(f"the run drifted {literal} units"),
                             literal)
        # real file-shaped tokens still are
        for token in ("r5.json", "data/out.csv", "results/R4.txt", "x.jsonl",
                      "cfg.yaml", "notes.md", "run.log", "script.py", "go.sh"):
            self.assertTrue(collect._attributable(f"see {token} for detail"), token)
        # a number glued to a data extension is still a file, not a decimal
        self.assertTrue(collect._attributable("see results2.json for detail"))

    def test_claim_verified_decimal_literal_error_taints_all(self):
        """Regression (bead 7369261c-a59): an error carrying only a decimal
        literal ('the run drifted 3.14 units') came back attributable (the
        '3.14' read as someone else's file), so C1 verified True instead of
        being tainted. An error nobody can own must taint EVERY claim."""
        errors = ["the run drifted 3.14 units"]
        self.assertFalse(collect.claim_verified({"id": "C1", "metric": "p99_ms"}, errors))
        self.assertFalse(collect.claim_verified({"id": "", "metric": ""}, errors))

    def test_claim_verified_whole_answer_verdicts_taint_no_one(self):
        """The README's whole-answer error families blame the answer's shape,
        not any one claim."""
        errors = ["claims_needed ids may be added, not removed",
                  "repro must not start with cd",
                  "run 'R1': wait_for 'R1' must be a file path or cmd:<command>",
                  "|effect| 0.8 <= 2 * noise_sd 0.75"]
        self.assertTrue(collect.claim_verified({"id": "C7", "metric": "m"}, errors))

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
