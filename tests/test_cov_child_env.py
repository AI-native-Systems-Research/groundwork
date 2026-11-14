"""Tests for tests/_cov_child_env.py and tests/_covsite/sitecustomize.py.

Subprocess coverage must not depend on where coverage is installed. The
env-isolation tests scrub HOME for their python children; on a
``pip install --user`` coverage the children resolve coverage (and the
a1_coverage.pth startup hook) through the user site-packages, i.e.
through $HOME — so the same suite that measures every child in CI
(system-site coverage) measures none of them on such a machine, and
new_study.py (exercised only by HOME-scrubbed children) reads 0% while
CI reads 85%. These tests pin both halves of the fix: covsite_env()
exports the keys, and a child started with them actually records data
even when its HOME points somewhere coverage cannot be found.
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cov_child_env  # noqa: E402
from _cov_child_env import covsite_env  # noqa: E402

HERE = Path(__file__).resolve().parent
COVSITE = HERE / "_covsite"
# The measured child. It is NOT under .coveragerc's source dirs, so a traced
# child records no rows for it — the test asserts the data file's EXISTENCE,
# which is the half that breaks on user-site coverage installs.
CHILD = HERE / "fixtures" / "covsite" / "child.py"


class CovsiteEnvKeys(unittest.TestCase):
    def setUp(self):
        # The tests below poke os.environ["PYTHONPATH"]; snapshot it so a
        # test can never erase an ambient value for later modules in the
        # same process (the hazard documented for cli_handlers).
        self.saved_pythonpath = os.environ.get("PYTHONPATH")

    def tearDown(self):
        if self.saved_pythonpath is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = self.saved_pythonpath

    def test_no_coverage_running_means_no_keys(self):
        # Outside a coverage run the helper must add nothing: the child env
        # stays as scrubbed as the caller built it.
        saved = {k: os.environ.get(k) for k in
                 ("COVERAGE_PROCESS_START", "COVERAGE_PROCESS_CONFIG")}
        try:
            os.environ.pop("COVERAGE_PROCESS_START", None)
            os.environ.pop("COVERAGE_PROCESS_CONFIG", None)
            self.assertEqual(covsite_env(), {})
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_under_coverage_exports_pythonpath_and_site(self):
        if not (os.environ.get("COVERAGE_PROCESS_START")
                or os.environ.get("COVERAGE_PROCESS_CONFIG")):
            self.skipTest("suite not running under coverage")
        # Without an ambient PYTHONPATH ours is the whole value...
        os.environ.pop("PYTHONPATH", None)
        self.assertEqual(covsite_env()["PYTHONPATH"].split(os.pathsep),
                         [str(COVSITE)])
        # ...and with one set (IDE runner, tox) it stays ahead of ours:
        # assert what we appended, never that we were the only writer.
        os.environ["PYTHONPATH"] = "/nonexistent/ambient"
        self.assertEqual(covsite_env()["PYTHONPATH"].split(os.pathsep),
                         ["/nonexistent/ambient", str(COVSITE)])
        keys = covsite_env()
        # COVERAGE_SITE must name a directory that actually contains
        # coverage, or the child-side fallback import cannot work.
        site_dir = keys.get("COVERAGE_SITE")
        self.assertTrue(site_dir, "COVERAGE_SITE missing")
        self.assertTrue(os.path.isdir(os.path.join(site_dir, "coverage")),
                        f"no coverage package under {site_dir}")

    def test_pythonpath_appends_when_caller_already_set_one(self):
        if not (os.environ.get("COVERAGE_PROCESS_START")
                or os.environ.get("COVERAGE_PROCESS_CONFIG")):
            self.skipTest("suite not running under coverage")
        os.environ["PYTHONPATH"] = "/nonexistent/caller/path"
        self.assertEqual(covsite_env()["PYTHONPATH"].split(os.pathsep),
                         ["/nonexistent/caller/path", str(COVSITE)])

    def test_pythonpath_is_not_duplicated_on_repeated_calls(self):
        if not (os.environ.get("COVERAGE_PROCESS_START")
                or os.environ.get("COVERAGE_PROCESS_CONFIG")):
            self.skipTest("suite not running under coverage")
        first = covsite_env()["PYTHONPATH"]
        os.environ["PYTHONPATH"] = first
        self.assertEqual(covsite_env()["PYTHONPATH"], first)
        # tearDown restores the ambient value; erasing PYTHONPATH here
        # would instead erase it for every later test in this process.


class ChildMeasuredWithScrubbedHome(unittest.TestCase):
    """A HOME-scrubbed child with the covsite keys must record data.

    This is the end-to-end half: it fails on the exact machine class the
    finding describes (coverage installed --user, HOME scrubbed) unless
    the env builder forwards the covsite keys — the child then imports
    coverage through COVERAGE_SITE, runs process_startup() from the
    sitecustomize on PYTHONPATH, and writes a data file. Skipped when the
    suite is not run under coverage (nothing to measure then).
    """

    def test_child_writes_data_file(self):
        if not os.environ.get("COVERAGE_PROCESS_START"):
            self.skipTest("suite not running under coverage run")
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            home = os.path.join(td, "home")
            os.makedirs(home)
            env = {"HOME": home, "PATH": "/usr/bin:/bin",
                   "COVERAGE_PROCESS_START": os.environ["COVERAGE_PROCESS_START"],
                   **covsite_env()}
            r = subprocess.run([sys.executable, str(CHILD)], cwd=td, env=env,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            data = [f for f in os.listdir(td) if f.startswith(".coverage.")]
            self.assertTrue(data, "child wrote no coverage data file; "
                                  "subprocess measurement is broken for "
                                  "HOME-scrubbed children again")


if __name__ == "__main__":
    unittest.main()
