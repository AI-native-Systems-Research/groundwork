"""Tests for skills/investigation/scripts/collect_spotlights.py.

Fixture tests/fixtures/spotlights/run-a/ is a small deep-research-shaped run
dir: result.json ({report: SpotlightReport, module_runs, ...}),
run_manifest.json (target.commit_sha is replaced by the test with the sha of
a tempdir git repo), and sorted/sorted_candidates.{md,json}. The ranking puts
cand-0002 first, so rank order (not result.json order) is exercised.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills/investigation/scripts"
SCRIPT = SCRIPTS / "collect_spotlights.py"
VALIDATOR = SCRIPTS / "validate_answer.py"
RUN = Path(__file__).resolve().parent / "fixtures" / "spotlights" / "run-a"

sys.path.insert(0, str(SCRIPTS))
import collect_spotlights as cs  # noqa: E402

CONSTANTS = "# toy constants\n\nBLOCK_SIZE = 16\nMAX_BLOCKS = 4096\n# 18%\n"
POLICY = ("class LRUPolicy:\n    \"\"\"Toy policy.\"\"\"\n\n"
          "    def evict(self, n):\n        out = []\n        for k in list(self.order):\n"
          "            out.append(k)\n        return out[:n]\n\n\n"
          "    def touch(self, k):\n        self.order.move_to_end(k)\n")


def git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env,
                          capture_output=True, text=True).stdout.strip()


def snapshot(root: Path) -> dict:
    return {str(p): (p.stat().st_mtime_ns, p.stat().st_size)
            for p in root.rglob("*") if p.is_file()}


class SpotlightsCollectTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        root = Path(self._td.name)
        # target repo (named toyrepo) with the cited files at one commit
        self.target = root / "toyrepo"
        (self.target / "toy/cache").mkdir(parents=True)
        (self.target / "toy/cache/constants.py").write_text(CONSTANTS)
        (self.target / "toy/cache/policy.py").write_text(POLICY)
        git(root, "init", "-q", str(self.target))
        git(self.target, "add", ".")
        git(self.target, "commit", "-qm", "c1")
        self.sha = git(self.target, "rev-parse", "HEAD")
        # a later commit moves the lines, so citations must come from the sha
        (self.target / "toy/cache/constants.py").write_text("# moved\n" + CONSTANTS)
        git(self.target, "commit", "-qam", "c2")
        # the study lives inside the target repo (default --repo = toplevel)
        self.study = self.target / "investigations" / "s1"
        self.study.mkdir(parents=True)
        # run dir copy with the manifest pointing at the first commit
        self.run_dir = root / "runs" / "spotlights-a"
        shutil.copytree(RUN, self.run_dir)
        mp = self.run_dir / "run_manifest.json"
        m = json.loads(mp.read_text())
        m["target"]["commit_sha"] = self.sha
        mp.write_text(json.dumps(m, indent=2))

    def tearDown(self):
        self._td.cleanup()

    def run_collect(self, *extra):
        return subprocess.run([sys.executable, str(SCRIPT), "--study", str(self.study),
                               "--run-dir", str(self.run_dir), *extra],
                              capture_output=True, text=True)

    def section(self):
        col = json.loads((self.study / "collection.json").read_text())
        self.assertEqual(col["collection_version"], 2)
        return col["spotlights"]

    def test_estimate_guard_blocks_every_numeric_spelling(self):
        """An estimate never becomes a claim value in ANY spelling: the guard
        set must carry the trimmed, padded, leading-dot and exponent forms of
        every numeric estimate, or a quoted source line spelled differently
        ('0.50' for the estimate 0.5) is picked as a claim value and passes
        all three guard layers."""
        spellings = {
            0.5: ["0.5", ".5", ".50", "0.50", "0.500", "5e-1", "5E-1", "5e-01",
                  "0.5e0", "0.5E0", "0.5e+0", "5.0e-1"],
            5.0: ["5", "5.0", "5.00", "5e0", "5E0", "5.0e0", "5e+0", "0.5e1"],
            10.0: ["10", "10.0", "1e1", "1.0e1", "1e+1", "1E1", "1e+01"],
            2.861085: ["2.861085", "2.861085e0", "0.2861085e1"],
        }
        unequal = {
            0.5: ["0.51", "0.49999", "2.5e-1", "0.499999", "0,5"],
            5.0: ["5.1", "4.999"],
            10.0: ["10.1", "1e2"],
            2.861085: ["2.8", "2.86", "2.861", "2.87"],
        }
        for value, forms in spellings.items():
            with self.subTest(estimate=value):
                forbidden = cs.estimate_values(
                    {"candidates": [{"estimated_impact": value}]}, None, None)
                for form in forms:
                    self.assertIn(form, forbidden, form)
                # unequal values stay pickable — the guard never over-blocks
                for near in unequal[value]:
                    self.assertNotIn(near, forbidden, near)
                # every emitted spelling parses back to exactly the estimate
                for form in forbidden:
                    self.assertEqual(float(form), float(value), form)
        # non-numeric estimates keep their exact-string shape only
        self.assertEqual(cs.estimate_values(
            {"candidates": [{"estimated_impact": "high"}]}, None, None), {"high"})

    def test_non_finite_estimates_do_not_crash_the_guard(self):
        """NaN/Infinity estimates (json.dump writes them by default and
        json.loads reads them back; nothing validates the field between the
        file read and the guard) must not raise: the "{f:e}" exponent pass
        has no exponent to partition and would ValueError on int(''),
        violating collect_run's CouldNotRun-only contract. Pinned to main's
        behavior — the forbidden set keeps the harmless base spellings
        ('NaN'/'nan', 'Infinity'/'inf') and the run still collects."""
        for v in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=v):
                spellings = cs.numeric_spellings(v)
                self.assertIn(json.dumps(v), spellings)
                self.assertIn(str(v), spellings)
                self.assertIn(repr(v), spellings)
        # finite spellings are untouched by the non-finite early return
        self.assertNotIn("nan", cs.numeric_spellings(0.5))
        self.assertEqual(cs.estimate_values(
            {"candidates": [{"estimated_impact": float("nan")}]}, None, None),
            {"NaN", "nan"})
        self.assertEqual(cs.estimate_values(
            {"candidates": [{"estimated_impact": float("inf")}]}, None, None),
            {"Infinity", "inf"})
        # end to end: a result.json carrying a NaN estimate collects cleanly
        # (at head pre-fix this raised ValueError inside collect_run)
        result = json.loads((self.run_dir / "result.json").read_text())
        result["report"]["candidates"][0]["estimated_impact"] = float("nan")
        (self.run_dir / "result.json").write_text(json.dumps(result))
        r = self.run_collect()
        self.assertEqual(r.returncode, 0, r.stderr)
        sec = self.section()
        # cand 0001's estimate is NaN, so its span must yield no claim whose
        # value spells the estimate — but the run itself collects, like main
        drafted_ids = [c["candidate_id"] for c in sec["claims_draft"]]
        self.assertIn("cand-toy_cache-0001", drafted_ids)  # BLOCK_SIZE = 16
        for c in sec["claims_draft"]:
            self.assertNotIn(c["value"], {"NaN", "nan"}, c)
        # collect_run's CouldNotRun-only contract holds for the same input
        sec2, _ = cs.collect_run(self.study, self.run_dir, top=5,
                                 repo=self.target)
        self.assertIn("claims_draft", sec2)

    def test_estimate_in_alternate_spelling_is_not_picked_as_a_value(self):
        """End to end through pick_value: a candidate whose span's first
        non-blank line is '0.50' against estimated_impact 0.5 must give a
        note and NO claim (at main the alternate spelling was drafted).
        The cited content must be written at the CITED sha, so this test
        rewrites the worktree file and amends a fresh commit on top."""
        result = json.loads((self.run_dir / "result.json").read_text())
        cand = result["report"]["candidates"][0]  # cand-toy_cache-0001
        cand["estimated_impact"] = 0.5
        (self.run_dir / "result.json").write_text(json.dumps(result))
        mp = self.run_dir / "run_manifest.json"
        m = json.loads(mp.read_text())

        def cite_sha(content):
            (self.target / "toy/cache/constants.py").write_text(content)
            git(self.target, "commit", "-qam", "c-quote")
            sha = git(self.target, "rev-parse", "HEAD")
            m["target"]["commit_sha"] = sha
            mp.write_text(json.dumps(m, indent=2))

        # the cited span is constants.py L3-L5: make every line of the span
        # the alternate spelling so pick_value has nothing else to pick
        cite_sha("x\nx\n0.50\n0.50\n0.50\n")
        r = self.run_collect()
        self.assertEqual(r.returncode, 0, r.stderr)
        sec = self.section()
        drafted = [c for c in sec["claims_draft"]
                   if c["candidate_id"] == "cand-toy_cache-0001"]
        self.assertEqual(drafted, [])
        self.assertTrue(any("cand-toy_cache-0001" in n and "no usable line" in n
                            for n in sec["notes"]))
        # the control: the canonical spelling '0.5' is equally blocked
        cite_sha("x\nx\n0.5\n0.5\n0.5\n")
        r = self.run_collect()
        self.assertEqual(r.returncode, 0, r.stderr)
        sec = self.section()
        drafted = [c for c in sec["claims_draft"]
                   if c["candidate_id"] == "cand-toy_cache-0001"]
        self.assertEqual(drafted, [])
        # an UNEQUAL value on the same lines stays pickable
        cite_sha("x\nx\n0.51\n0.51\n0.51\n")
        r = self.run_collect()
        self.assertEqual(r.returncode, 0, r.stderr)
        sec = self.section()
        drafted = [c for c in sec["claims_draft"]
                   if c["candidate_id"] == "cand-toy_cache-0001"]
        self.assertEqual([c["value"] for c in drafted], ["0.51"])

    def test_copies_and_citations(self):
        r = self.run_collect()
        self.assertEqual(r.returncode, 0, r.stderr)
        sp = self.study / "spotlights"
        for name in ("result.json", "run_manifest.json", "sorted_candidates.md",
                     "sorted_candidates.json"):
            self.assertTrue((sp / name).is_file(), name)
        self.assertEqual((sp / "result.json").read_bytes(),
                         (self.run_dir / "result.json").read_bytes())
        sec = self.section()
        self.assertEqual(sec["run_id"], "run-toy0001")
        self.assertEqual(sec["target"]["cited_sha"], self.sha)
        self.assertEqual(sec["rank_source"], "sorted_candidates.json")
        claims = sec["claims_draft"]
        # ranked: 0002, 0001, 0003 cited; 0004's file doesn't exist -> note only
        self.assertEqual([c["candidate_id"] for c in claims],
                         ["cand-toy_cache-0002", "cand-toy_cache-0001", "cand-toy_cache-0003"])
        self.assertTrue(any("cand-toy_cache-0004" in n and "not found" in n for n in sec["notes"]))
        c2 = claims[0]
        self.assertEqual(c2["value"], "def evict(self, n):")
        self.assertEqual(c2["condition"], f"toyrepo @ {self.sha[:8]}, toy/cache/policy.py:L4")
        self.assertEqual(c2["evidence"], "citations/cite-cand-toy_cache-0002.txt")
        self.assertTrue(c2["verified"])
        text = (self.study / c2["evidence"]).read_text()
        header, _, body = text.partition("\n")
        self.assertEqual(header, f"toyrepo@{self.sha}:toy/cache/policy.py:L4-L9")
        self.assertEqual(body, "".join(POLICY.splitlines(keepends=True)[3:9]))
        c1 = claims[1]
        self.assertEqual(c1["value"], "BLOCK_SIZE = 16")  # from the sha, not HEAD
        for c in claims:
            self.assertIn(c["value"], (self.study / c["evidence"]).read_text())

    def test_no_estimate_becomes_a_claim_value(self):
        r = self.run_collect("--top", "10")
        self.assertEqual(r.returncode, 0, r.stderr)
        result = json.loads((self.run_dir / "result.json").read_text())
        ranked = json.loads((self.run_dir / "sorted/sorted_candidates.json").read_text())
        estimates = set()
        for c in result["report"]["candidates"]:
            estimates.add(c["estimated_impact"])
            estimates.add(c["estimated_impact_explanation"])
            for p in c["proposals"]:
                estimates.add(p["title"])
                if p.get("expected_effect"):
                    estimates.add(p["expected_effect"])
        estimates.add(result["report"]["run"]["cost_usd"])
        for c in ranked["candidates"]:
            estimates.update(v for v in (c["score"], c["rationale"], c["impact"]) if v is not None)
        claims = self.section()["claims_draft"]
        self.assertTrue(claims)
        for c in claims:
            self.assertNotIn(c["value"], estimates, c)
            self.assertIsInstance(c["value"], str)
            for e in estimates:
                if isinstance(e, (int, float)):
                    self.assertNotIn(str(e), c["value"], c)

    def test_claims_pass_validator_with_check_citations(self):
        r = self.run_collect()
        self.assertEqual(r.returncode, 0, r.stderr)
        claims = self.section()["claims_draft"]
        (self.study / "final").mkdir()
        (self.study / "deliverable.md").write_text("x\n")
        answer = {"answer": "a", "deliverable": "deliverable.md", "repro": "r",
                  "claims": [{k: c[k] for k in ("id", "metric", "value", "condition",
                                                "evidence")} for c in claims]}
        ap = self.study / "final" / "answer.json"
        ap.write_text(json.dumps(answer))
        v = subprocess.run([sys.executable, str(VALIDATOR), str(ap), "--check-citations",
                            "--repo", f"toyrepo={self.target}"],
                           capture_output=True, text=True)
        self.assertEqual(v.returncode, 0, v.stdout + v.stderr)

    def test_top_n(self):
        r = self.run_collect("--top", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        claims = self.section()["claims_draft"]
        self.assertEqual([c["candidate_id"] for c in claims], ["cand-toy_cache-0002"])
        self.assertEqual(sorted(p.name for p in (self.study / "citations").iterdir()),
                         ["cite-cand-toy_cache-0002.txt"])

    def test_run_dir_is_read_only(self):
        before = snapshot(self.run_dir)
        r = self.run_collect()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(snapshot(self.run_dir), before)

    def test_merge_keeps_other_keys(self):
        (self.study / "collection.json").write_text(json.dumps(
            {"collection_version": 2, "nous": {"run_id": "n"},
             "telemetry": {"cap-a": {"usable": True}}}))
        r = self.run_collect()
        self.assertEqual(r.returncode, 0, r.stderr)
        col = json.loads((self.study / "collection.json").read_text())
        self.assertEqual(col["nous"], {"run_id": "n"})
        self.assertEqual(col["telemetry"], {"cap-a": {"usable": True}})
        self.assertIn("claims_draft", col["spotlights"])

    def test_unknown_sha_gives_note_no_claims(self):
        r = self.run_collect("--sha", "0" * 40)
        self.assertEqual(r.returncode, 0, r.stderr)
        sec = self.section()
        self.assertEqual(sec["claims_draft"], [])
        self.assertTrue(any("not a commit" in n for n in sec["notes"]))
        self.assertFalse((self.study / "citations").exists())

    def test_proposals_listed_as_hypotheses(self):
        self.assertEqual(self.run_collect().returncode, 0)
        top = self.section()["candidates_top"]
        first = top[0]
        self.assertEqual(first["id"], "cand-toy_cache-0002")
        self.assertEqual(first["proposals_are_hypotheses"][0]["title"], "Add S3-FIFO policy")

    def test_bare_signal_pipeline_report(self):
        result = json.loads((self.run_dir / "result.json").read_text())
        (self.run_dir / "result.json").unlink()
        (self.run_dir / "spotlight_report.json").write_text(json.dumps(result["report"]))
        r = self.run_collect()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.study / "spotlights" / "result.json").is_file())
        self.assertEqual(len(self.section()["claims_draft"]), 3)

    def test_missing_result_is_exit_1(self):
        (self.run_dir / "result.json").unlink()
        r = self.run_collect()
        self.assertEqual(r.returncode, 1)
        self.assertFalse((self.study / "collection.json").exists())


if __name__ == "__main__":
    unittest.main()
