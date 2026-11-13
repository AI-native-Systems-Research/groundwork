"""In-process tests for collect_telemetry.py's error and guard branches.

test_collect_telemetry.py drives the script as a subprocess against the
hand-built cap-a fixture, so it covers the happy path end to end (87% of
the file). The branches left dead are the refusal and degradation paths:
load_capture's unreadable/non-object rejections, verify_integrity's
recorded-both-ways skip and malformed moved entry, build_summary's
missing/unreadable prom file, describe_cluster's unnamed-cluster forms,
draft_claims' None-stat skip, and main()'s not-a-directory and bad
--capture-id exits. Those branches decide between "record a note and
keep collecting" and "refuse the capture", so a regression in them
silently changes what a study's claims say.

These tests import the module in-process (the house pattern of
tests/test_doctor_inprocess.py / tests/test_store_guards.py — no
subprocess, no env scrubbing, every line counts) and drive the branch
conditions directly with small capture dicts built in a tempdir. The
fixture cap-a is reused only where its real sha256s matter.

Run from the repo root:  python3 -m unittest tests.test_collect_telemetry_branches -v
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "investigation" / "scripts"
SCRIPT = SCRIPTS / "collect_telemetry.py"
CAP = Path(__file__).resolve().parent / "fixtures" / "telemetry" / "cap-a"

sys.path.insert(0, str(SCRIPTS))
import collect_telemetry as CT  # noqa: E402


def run_main(study, capture_id):
    return subprocess.run([sys.executable, str(SCRIPT), "--study", str(study),
                           "--capture-id", capture_id], capture_output=True, text=True)


class LoadCaptureTest(unittest.TestCase):
    """load_capture's three refusal branches (76-81)."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_no_capture_json(self):
        with self.assertRaisesRegex(CT.CouldNotRun, "no capture.json in"):
            CT.load_capture(self.dir)

    def test_unreadable_capture_json(self):
        p = self.dir / "capture.json"
        p.write_text("{}\n")
        p.chmod(0o000)
        try:
            with self.assertRaisesRegex(CT.CouldNotRun, "unreadable"):
                CT.load_capture(self.dir)
        finally:
            p.chmod(0o644)

    def test_unparseable_capture_json(self):
        (self.dir / "capture.json").write_text("{not json")
        with self.assertRaisesRegex(CT.CouldNotRun, "unreadable"):
            CT.load_capture(self.dir)

    def test_non_object_capture_json(self):
        (self.dir / "capture.json").write_text("[1, 2]")
        with self.assertRaisesRegex(CT.CouldNotRun, "is not a JSON object"):
            CT.load_capture(self.dir)


class VerifyIntegrityTest(unittest.TestCase):
    """verify_integrity's recorded-both-ways skip (108) and malformed
    moved entries (111-113)."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)
        (self.dir / "prom").mkdir()
        (self.dir / "prom" / "q.json").write_text("{}\n")

    def tearDown(self):
        self._td.cleanup()

    def cap(self, **over):
        cap = {"files": {"prom/q.json": "0" * 64}, "moved": {}}
        cap.update(over)
        return cap

    def test_recorded_both_ways_is_checked_once_via_moved(self):
        """A rel recorded in both files and moved, with the local copy gone
        (the size guard's shape): the first loop skips it (108) and the
        moved loop checks the relocated file, so it is checked exactly
        once and the capture stays ok."""
        runs = self.dir.parent / "runs"
        runs.mkdir(exist_ok=True)
        dest = runs / "q.json"
        (self.dir / "prom" / "q.json").rename(dest)
        cap = self.cap(files={"prom/q.json": "irrelevant-under-files"},
                       moved={"prom/q.json": {"sha256": CT.sha256_file(dest),
                                              "path": str(dest)}})
        out = CT.verify_integrity(self.dir, cap)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["checked"], ["prom/q.json"])
        self.assertEqual(out["missing"], [])

    def test_moved_entry_without_path_is_missing(self):
        cap = self.cap(files={}, moved={"prom/gone.json": {"sha256": "0" * 64}})
        out = CT.verify_integrity(self.dir, cap)
        self.assertFalse(out["ok"])
        self.assertEqual(out["missing"], [{"file": "prom/gone.json", "path": None}])
        self.assertEqual(out["checked"], [])

    def test_moved_entry_not_a_dict_is_missing(self):
        cap = self.cap(files={}, moved={"prom/gone.json": "elsewhere"})
        out = CT.verify_integrity(self.dir, cap)
        self.assertEqual(out["missing"], [{"file": "prom/gone.json", "path": None}])


class BuildSummaryTest(unittest.TestCase):
    """build_summary's missing/unreadable prom file paths (137-142)."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)
        # build_summary's missing-file path goes through prom_path, which
        # falls back to reading capture.json for a moved entry — so every
        # scenario needs a readable capture.json present.
        (self.dir / "capture.json").write_text("{}\n")

    def tearDown(self):
        self._td.cleanup()

    def test_missing_prom_file_becomes_error_entry(self):
        cap = {"queries": {"q": "up"}, "failed_queries": {}}
        notes = []
        out = CT.build_summary(self.dir, cap, "c1", notes)
        self.assertEqual(out["queries"]["q"]["error"],
                         f"prom/q.json not found ({self.dir / 'prom' / 'q.json'})")
        self.assertTrue(any("not found" in n for n in notes), notes)
        self.assertEqual(out["queries"]["q"]["overall"]["n_points"], 0)

    def test_unreadable_prom_file_becomes_error_entry(self):
        (self.dir / "prom").mkdir()
        p = self.dir / "prom" / "q.json"
        p.write_text("{}\n")
        p.chmod(0o000)
        try:
            cap = {"queries": {"q": "up"}, "failed_queries": {}}
            notes = []
            out = CT.build_summary(self.dir, cap, "c1", notes)
            self.assertIn("unreadable", out["queries"]["q"]["error"])
            self.assertTrue(any("unreadable" in n for n in notes), notes)
        finally:
            p.chmod(0o644)

    def test_unparseable_prom_file_becomes_error_entry(self):
        (self.dir / "prom").mkdir()
        (self.dir / "prom" / "q.json").write_text("{not json")
        cap = {"queries": {"q": "up"}, "failed_queries": {}}
        notes = []
        out = CT.build_summary(self.dir, cap, "c1", notes)
        self.assertIn("unreadable", out["queries"]["q"]["error"])

    def test_failed_query_at_capture_skips_the_file(self):
        cap = {"queries": {"q": "up"}, "failed_queries": {"q": "HTTP 422: bad_data"}}
        notes = []
        out = CT.build_summary(self.dir, cap, "c1", notes)
        self.assertEqual(out["queries"]["q"]["error"], "HTTP 422: bad_data")
        self.assertTrue(any("failed at capture" in n for n in notes), notes)


class DescribeClusterTest(unittest.TestCase):
    """describe_cluster's unnamed-cluster forms (157-159)."""

    def test_named_cluster(self):
        self.assertEqual(CT.describe_cluster({"cluster": "prod"}), "cluster prod")

    def test_unnamed_with_source_host(self):
        self.assertEqual(CT.describe_cluster({"source": {"host": "h:9090"}}),
                         "cluster unnamed (source h:9090)")

    def test_unnamed_with_non_dict_source(self):
        self.assertEqual(CT.describe_cluster({"source": "url"}),
                         "cluster unnamed")

    def test_fully_unnamed(self):
        self.assertEqual(CT.describe_cluster({}), "cluster unnamed")


class DraftClaimsTest(unittest.TestCase):
    """draft_claims' None-stat skip (183): a stat with no value emits no claim."""

    def test_missing_stat_is_skipped_not_crashed(self):
        summary = {"queries": {"q": {"overall": {"n_points": 3, "n_series": 1,
                                                 "mean": 1.5, "p50": None},
                                     "query": "up"}}}
        claims = CT.draft_claims("c1", {"window": {}}, summary, json.dumps(summary),
                                 {"mismatches": [], "missing": []}, [])
        # p50 is None -> skipped; mean still drafts with its value.
        self.assertEqual([c["metric"] for c in claims], ["q mean"])
        self.assertEqual(claims[0]["value"], 1.5)


class MainRefusalsTest(unittest.TestCase):
    """main()'s --study not-a-directory and bad --capture-id exits
    (266-271): exit 1, a message on stderr, nothing written."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.study = Path(self._td.name) / "study"
        self.study.mkdir()

    def tearDown(self):
        self._td.cleanup()

    def test_study_not_a_directory(self):
        r = run_main(self.study / "nope", "cap-a")
        self.assertEqual(r.returncode, 1)
        self.assertIn("is not a directory", r.stderr)

    def test_bad_capture_id_with_slash(self):
        r = run_main(self.study, "a/b")
        self.assertEqual(r.returncode, 1)
        self.assertIn("bad --capture-id 'a/b'", r.stderr)

    def test_bad_capture_id_dot(self):
        r = run_main(self.study, ".")
        self.assertEqual(r.returncode, 1)
        self.assertIn("bad --capture-id", r.stderr)

    def test_bad_capture_id_empty(self):
        r = run_main(self.study, "")
        self.assertEqual(r.returncode, 1)
        self.assertIn("bad --capture-id", r.stderr)

    def test_refusals_write_nothing(self):
        for r in (run_main(self.study / "nope", "cap-a"),
                  run_main(self.study, ".."),
                  run_main(self.study, "")):
            self.assertEqual(r.returncode, 1)
            self.assertFalse((self.study / "collection.json").exists())


class CollectDegradedTest(unittest.TestCase):
    """collect() through a degraded capture: an unreadable prom file must
    downgrade the query to an error entry (not fail the whole capture), and
    the missing file must surface as a note. Covers the branches above in
    their real caller, subprocess-free."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.study = Path(self._td.name) / "study"
        self.study.mkdir()

    def tearDown(self):
        self._td.cleanup()

    def test_missing_prom_file_is_a_note_and_no_claim(self):
        (self.study / "telemetry" / "cap-b").mkdir(parents=True)
        (self.study / "telemetry" / "cap-b" / "capture.json").write_text(json.dumps(
            {"queries": {"q": "up"}, "failed_queries": {}, "files": {}, "moved": {}}))
        out = CT.collect(self.study, "cap-b")
        self.assertTrue(out["usable"])
        self.assertTrue(any("not found" in n for n in out["notes"]), out["notes"])
        self.assertEqual(out["claims_draft"], [])
        summary = json.loads((self.study / "telemetry" / "cap-b" / "summary.json").read_text())
        self.assertIn("error", summary["queries"]["q"])

    def test_capture_dir_not_a_directory(self):
        with self.assertRaisesRegex(CT.CouldNotRun, "is not a directory"):
            CT.collect(self.study, "cap-b")


if __name__ == "__main__":
    unittest.main()
