"""In-process tests for collect_spotlights.py's refusal and skip branches.

tests/test_collect_spotlights.py drives the script as a subprocess against
the run-a fixture, so the happy path is covered end to end (90% of the
file). The branches left dead decide between "record a note and keep
collecting" and "refuse the run": report_of's no-report refusal, the
unreadable/non-JSON result.json guards, the citation-loop skip tiers
(no span, path not at the sha, lines out of range, no usable line to
quote), the source_root alternate-path retry, the missing-manifest and
missing-sha notes, and collect_run's four CouldNotRun refusals.

These tests import the module in-process (the house pattern of
tests/test_doctor_inprocess.py / tests/test_collect_guards.py — no
subprocess, no env scrubbing, every line counts) and build minimal
deep-research-shaped run dirs in a tempdir git repo, so every skip tier
is reached without fabricating a whole engine output. Each test pins the
note (or the refusal) the collector must produce for that shape, because
a regression in any of them silently changes what a study's claims say:
a skipped citation is a claim that never exists.

Run from the repo root:  python3 -m unittest tests.test_collect_spotlights_branches -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills/investigation/scripts"
SCRIPT = SCRIPTS / "collect_spotlights.py"

sys.path.insert(0, str(SCRIPTS))
import collect_spotlights as cs  # noqa: E402


def git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@e",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@e")
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env,
                          capture_output=True, text=True).stdout.strip()


class Base(unittest.TestCase):
    """A tempdir git repo (the target) with a study inside it, plus a run
    dir holding a minimal deep-research-shaped result."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.repo = self.root / "toyrepo"
        (self.repo / "toy").mkdir(parents=True)
        (self.repo / "toy" / "c.py").write_text("X = 1\nY = 2\n")
        git(self.root, "init", "-q", str(self.repo))
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "c1")
        self.sha = git(self.repo, "rev-parse", "HEAD")
        self.study = self.repo / "investigations" / "s1"
        self.study.mkdir(parents=True)
        self.run_dir = self.root / "runs" / "spotlights-b"
        self.run_dir.mkdir(parents=True)

    def cand(self, cid, file="toy/c.py", start=1, end=1, **extra):
        loc = {"file": file, "spans": [{"line_start": start, "line_end": end,
                                        "symbol": "S"}]}
        c = {"id": cid, "locations": [loc]}
        c.update(extra)
        return c

    def write_result(self, candidates, source_root=None, run=None):
        report = {"candidates": candidates,
                  "run": run if run is not None else {"run_id": "r1"}}
        if source_root is not None:
            report["project_tree"] = {"repository": {"source_root": source_root}}
        (self.run_dir / "result.json").write_text(json.dumps({"report": report}))

    def write_manifest(self, sha=None):
        m = {"target": {"commit_sha": sha if sha is not None else self.sha}}
        (self.run_dir / "run_manifest.json").write_text(json.dumps(m))

    def collect(self, **kw):
        kw.setdefault("repo", self.repo)
        return cs.collect(self.study, self.run_dir, kw.pop("top", 5),
                          kw.pop("repo"), kw.pop("sha", None))


class ResultReadingTest(Base):
    """report_of's no-report refusal and _load_json's guard (80-81, 98)."""

    def test_report_of_refuses_a_dict_without_report_or_candidates(self):
        with self.assertRaisesRegex(cs.CouldNotRun, "no SpotlightReport"):
            cs.report_of({"module_runs": [], "manager_issues": []})

    def test_report_of_takes_the_bare_signal_pipeline_shape(self):
        bare = {"candidates": [{"id": "c"}], "run": {"run_id": "r"}}
        self.assertIs(cs.report_of(bare), bare)

    def test_load_json_returns_none_on_bad_json(self):
        p = self.root / "bad.json"
        p.write_text("{not json")
        self.assertIsNone(cs._load_json(p))

    def test_load_json_returns_none_on_unreadable_path(self):
        d = self.root / "a-dir.json"
        d.mkdir()  # read_text on a directory raises OSError
        self.assertIsNone(cs._load_json(d))


class ResultGuardsTest(Base):
    """collect()'s result.json guards: unparseable is a refusal (292), a
    directory named result.json hides the file from find_result (89)."""

    def test_result_json_not_valid_json_refuses(self):
        (self.run_dir / "result.json").write_text("{not json")
        with self.assertRaisesRegex(cs.CouldNotRun, "is not valid JSON"):
            self.collect()

    def test_result_json_as_directory_is_not_a_result(self):
        # an unreadable result.json: find_result only accepts files, so the
        # collector refuses with the documented no-result message.
        (self.run_dir / "result.json").mkdir()
        with self.assertRaisesRegex(cs.CouldNotRun, "no result.json"):
            self.collect()


class MissingInputsNotesTest(Base):
    """The degraded-run notes: no manifest (299), no sha (334)."""

    def test_no_manifest_is_a_note_and_cited_sha_stays_none(self):
        self.write_result([self.cand("cand-t-0001")])
        sec = self.collect()
        self.assertTrue(any("no readable run_manifest.json" in n for n in sec["notes"]))
        self.assertIsNone(sec["target"]["cited_sha"])

    def test_empty_manifest_sha_is_a_note_and_no_citations(self):
        self.write_result([self.cand("cand-t-0001")])
        self.write_manifest(sha="")
        sec = self.collect()
        self.assertTrue(any("no target commit sha" in n for n in sec["notes"]))
        self.assertEqual(sec["claims_draft"], [])
        self.assertFalse((self.study / "citations").exists())


class CitationSkipTiersTest(Base):
    """The citation loop's skip tiers: out-of-range lines (362-364),
    no-file-span (349-351), no usable line to quote (367-369)."""

    def test_out_of_range_span_is_a_note_and_no_claim(self):
        self.write_result([self.cand("cand-t-0001", start=9, end=12)])
        self.write_manifest()
        sec = self.collect()
        self.assertEqual(sec["claims_draft"], [])
        self.assertTrue(any("out of range" in n and "cand-t-0001" in n
                            for n in sec["notes"]), sec["notes"])
        top = sec["candidates_top"][0]
        self.assertFalse(top["cited"])

    def test_candidate_without_a_file_span_is_a_note(self):
        self.write_result([{"id": "cand-t-0003"},
                           {"id": "cand-t-0004",
                            "locations": [{"spans": [{"line_start": 1,
                                                      "line_end": 1}]}]}])
        self.write_manifest()
        sec = self.collect()
        self.assertEqual(sec["claims_draft"], [])
        self.assertEqual(len([n for n in sec["notes"] if "no file span to cite" in n]),
                         2)

    def test_span_whose_only_line_is_the_estimate_has_no_usable_value(self):
        # the quoted span's single line IS the estimate, so pick_value has
        # nothing left to quote: a note, not a crash, and no claim.
        self.write_result([self.cand("cand-t-0001", start=1, end=1,
                                     estimated_impact="X = 1")])
        self.write_manifest()
        sec = self.collect()
        self.assertEqual(sec["claims_draft"], [])
        self.assertTrue(any("no usable line to quote" in n for n in sec["notes"]),
                        sec["notes"])

    def test_junk_candidate_rows_are_skipped_everywhere(self):
        # a non-dict candidate row must be skipped by the report scan and
        # the ranking, not crash the collection.
        self.write_result(["not-a-mapping", self.cand("cand-t-0001")])
        self.write_manifest()
        sec = self.collect()
        self.assertEqual(sec["n_candidates"], 1)
        self.assertEqual([c["candidate_id"] for c in sec["claims_draft"]],
                         ["cand-t-0001"])


class SourceRootRetryTest(Base):
    """The source_root alternate-path retry (353-356): engine paths are
    relative to the repo's source_root, git paths to the repo root."""

    def test_source_root_prefix_is_stripped_when_citing(self):
        # the candidate's file is source-root-relative; the citation must
        # resolve against the repo root and record the full path.
        self.write_result([self.cand("cand-t-0001", file="c.py")],
                          source_root="toy")
        self.write_manifest()
        sec = self.collect()
        self.assertEqual(len(sec["claims_draft"]), 1)
        claim = sec["claims_draft"][0]
        self.assertEqual(claim["value"], "X = 1")
        self.assertIn("toy/c.py:L1", claim["condition"])
        header = (self.study / "citations" / "cite-cand-t-0001.txt").read_text()
        self.assertTrue(header.startswith(f"toyrepo@{self.sha}:toy/c.py:L1-L1\n"),
                        header)

    def test_source_root_retry_falls_back_to_the_original_path(self):
        # when the source_root-prefixed path does not exist either, the
        # note names the original (unprefixed) path.
        self.write_result([self.cand("cand-t-0001", file="missing.py")],
                          source_root="toy")
        self.write_manifest()
        sec = self.collect()
        self.assertEqual(sec["claims_draft"], [])
        self.assertTrue(any("missing.py not found at" in n for n in sec["notes"]),
                        sec["notes"])


class GitHelpersTest(Base):
    """first_span's guard arms (265, 269) and git_bytes's OSError face
    (246-247)."""

    def test_first_span_without_locations(self):
        self.assertEqual(cs.first_span({"id": "x"}), (None, None))

    def test_first_span_location_without_file(self):
        self.assertEqual(cs.first_span(
            {"locations": [{"spans": [{"line_start": 1, "line_end": 1}]}]}),
            (None, None))

    def test_first_span_span_without_line_start(self):
        self.assertEqual(cs.first_span(
            {"locations": [{"file": "f", "spans": [{"line_end": 1}]}]}),
            (None, None))

    def test_git_bytes_without_git_on_path_returns_none(self):
        # git missing (OSError) must read as "could not run git", never a
        # traceback — the same face a PATHEXT-less environment produces.
        env = dict(os.environ, PATH=str(self.root / "emptybin"))
        (self.root / "emptybin").mkdir()
        r = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r); import collect_spotlights as cs; "
             "print(cs.git_bytes(%r, 'rev-parse', 'HEAD'))" % (
                 str(SCRIPTS), str(self.repo))],
            capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "None")


class RankingTest(Base):
    """ranked_ids's md-only order (116-117) and estimate_values's junk-row
    skips (223, 265)."""

    def test_md_order_ranks_by_first_appearance(self):
        cands = [{"id": "cand-x-0001"}, {"id": "cand-x-0002"}]
        md = "# sorted\ncand-x-0002 is first, cand-x-0001 second\n"
        order, source = cs.ranked_ids(cands, None, md)
        self.assertEqual(order, ["cand-x-0002", "cand-x-0001"])
        self.assertEqual(source, "sorted_candidates.md (order of first appearance)")

    def test_sorted_json_junk_rows_are_skipped(self):
        # a junk row in sorted_candidates.json is skipped, and an id the
        # report does not carry is dropped (only known candidates rank).
        order, source = cs.ranked_ids([{"id": "a"}, {"id": "b"}],
                                      {"candidates": ["junk", {"id": "b"}]}, None)
        self.assertEqual(order, ["b", "a"])
        self.assertEqual(source, "sorted_candidates.json")

    def test_estimate_values_skips_non_dict_sorted_rows(self):
        vals = cs.estimate_values({"candidates": []},
                                  {"candidates": ["junk", {"score": 7}]}, None)
        self.assertIn("7", vals)

    def test_root_level_sorted_files_are_copied_and_rank(self):
        # find_sorted also looks at the run-dir root (not only sorted/):
        # both files are copied and the json wins the ranking.
        self.write_result([self.cand("cand-t-0001")])
        self.write_manifest()
        (self.run_dir / "sorted_candidates.md").write_text("cand-t-0001\n")
        (self.run_dir / "sorted_candidates.json").write_text(json.dumps(
            {"candidates": [{"id": "cand-t-0001", "score": 91}]}))
        sec = self.collect()
        self.assertEqual(sec["rank_source"], "sorted_candidates.json")
        self.assertEqual(sorted(sec["copies"]),
                         ["spotlights/result.json", "spotlights/run_manifest.json",
                          "spotlights/sorted_candidates.json",
                          "spotlights/sorted_candidates.md"])


class NumericSpellingsGuardTest(unittest.TestCase):
    """numeric_spellings' non-numeric/bool guard (150) and _parses_exactly's
    ValueError face (205-206)."""

    def test_non_numbers_and_bools_have_no_spellings(self):
        self.assertEqual(cs.numeric_spellings("high"), set())
        self.assertEqual(cs.numeric_spellings(True), set())
        self.assertEqual(cs.numeric_spellings(None), set())

    def test_parses_exactly_drops_non_parseable_forms(self):
        self.assertFalse(cs._parses_exactly("nan", 0.5))
        self.assertFalse(cs._parses_exactly("not a number", 0.5))
        self.assertTrue(cs._parses_exactly("0.5", 0.5))


class CollectRunRefusalsTest(Base):
    """collect_run's four CouldNotRun refusals (432, 434, 440, 445) and the
    CollectionError pass-through from a bad collection.json."""

    def test_study_not_a_directory(self):
        with self.assertRaisesRegex(cs.CouldNotRun, "--study .* is not a directory"):
            cs.collect_run(self.root / "nope", self.run_dir)

    def test_run_dir_not_a_directory(self):
        with self.assertRaisesRegex(cs.CouldNotRun, "--run-dir .* is not a directory"):
            cs.collect_run(self.study, self.root / "nope")

    def test_repo_flag_not_a_git_repository(self):
        plain = self.root / "plain"
        plain.mkdir()
        with self.assertRaisesRegex(cs.CouldNotRun, "is not a git repository"):
            cs.collect_run(self.study, self.run_dir, repo=plain)

    def test_study_outside_any_git_repo_without_repo(self):
        outside = self.root / "outside"
        (outside / "study").mkdir(parents=True)
        with self.assertRaisesRegex(cs.CouldNotRun, "is not inside a git repo; pass --repo"):
            cs.collect_run(outside / "study", self.run_dir)

    def test_bad_collection_json_is_refused_not_overwritten(self):
        self.write_result([self.cand("cand-t-0001")])
        self.write_manifest()
        (self.study / "collection.json").write_text("[1, 2]")
        with self.assertRaisesRegex(Exception, "not a JSON object"):
            cs.collect_run(self.study, self.run_dir)
        self.assertEqual((self.study / "collection.json").read_text(), "[1, 2]")

    def test_main_refusals_exit_1_without_a_traceback(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPT),
             "--study", str(self.root / "nope"), "--run-dir", str(self.run_dir)],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("is not a directory", r.stderr)
        self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
