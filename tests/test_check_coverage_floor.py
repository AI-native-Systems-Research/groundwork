"""Tests for scripts/check_coverage_floor.py — the per-file coverage floor.

The total floor (fail_under in .coveragerc) cannot catch "one file at 0%":
at ~90% total a 126-statement file dropping to 0% moves the total by under 2
points and the build stays green — which is how issues #2, #3 and #8 each
went unnoticed until a manual audit. These tests drive the checker against
real coverage data written in a tempdir, one case per behaviour: a file below
the floor fails, a file exactly at the floor passes, a never-imported file
reads 0% and fails, zero-statement files are skipped, missing data is an
error, and --rcfile/--data-file route to the right data.

Run from the repo root:  python3 -m unittest tests.test_check_coverage_floor -v
"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check_coverage_floor.py"

# The checker drives `coverage` via its API, so the interpreter running the
# tests must have it installed (CI's unit job pip-installs it alongside).
# The tests import the module and drive main() in-process (house style, same
# as tests/test_check_skill_spec.py) so the checker itself is measured.
try:
    import coverage  # noqa: F401
    HAVE_COVERAGE = True
except ImportError:
    HAVE_COVERAGE = False

sys.path.insert(0, str(REPO / "scripts"))
import check_coverage_floor as ccf  # noqa: E402


def run_checker(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    """Drive ccf.main() in a subprocess-like wrapper: chdir to `cwd` (the
    checker reads data from the cwd's .coveragerc/… when no flag routes it),
    capture stdout/stderr, and return a CompletedProcess-alike."""
    class R:
        pass
    r = R()
    old = Path.cwd()
    out, err = io.StringIO(), io.StringIO()
    try:
        os.chdir(cwd)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            r.returncode = ccf.main([*args])
    finally:
        os.chdir(old)
    r.stdout, r.stderr = out.getvalue(), err.getvalue()
    return r


@unittest.skipUnless(HAVE_COVERAGE, "the checker needs coverage installed")
class Base(unittest.TestCase):
    """A tempdir whose coverage data measures a fixed spread of files.

    runner.py imports everything and calls only mod.a(), so after
    `coverage run --source=. runner.py` the data holds:
      mod.py 75% (b() never called), dead.py 50% (d() never called),
      pkg/m.py 100%, pkg/__init__.py 0 statements, runner.py 100%.
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.dir = Path(self._td.name)
        (self.dir / "mod.py").write_text(
            "def a():\n    return 1\n\n\ndef b():\n    return 2\n",
            encoding="utf-8")
        (self.dir / "dead.py").write_text(
            "def d():\n    return 1\n", encoding="utf-8")
        (self.dir / "pkg").mkdir()
        (self.dir / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (self.dir / "pkg" / "m.py").write_text("x = 1\n", encoding="utf-8")
        (self.dir / "runner.py").write_text(
            "import sys\nsys.path.insert(0, '.')\nimport mod, dead, pkg.m\nmod.a()\n",
            encoding="utf-8")
        self.measure()

    def measure(self) -> None:
        """(Re)run coverage over the tempdir; --source=. records every file
        in it, including ones the runner never imports."""
        r = subprocess.run(
            [sys.executable, "-m", "coverage", "run", "--source=.", "runner.py"],
            capture_output=True, text=True, cwd=str(self.dir))
        self.assertEqual(r.returncode, 0, r.stderr)


class AtOrAboveFloor(Base):
    def test_file_exactly_at_floor_passes(self):
        # dead.py measures exactly 50%: at the floor, not below it.
        r = run_checker("50", cwd=self.dir)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("OK per-file floor 50%", r.stdout)
        self.assertIn("dead.py", r.stdout)

    def test_file_above_floor_passes(self):
        r = run_checker("40", cwd=self.dir)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_default_floor_is_40(self):
        r = run_checker(cwd=self.dir)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("floor 40%", r.stdout)


class BelowFloor(Base):
    def test_file_below_floor_fails(self):
        r = run_checker("55", cwd=self.dir)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("1 file(s) below", r.stderr)
        self.assertIn("dead.py", r.stderr)
        self.assertIn("50.0%", r.stderr)

    def test_never_imported_file_reads_zero_and_fails(self):
        # dead2.py exists in the source tree but the runner never imports it:
        # --source=. still measures it, at 0% — the regression shape of
        # issues #2/#3/#8 (file present, no test executes it).
        (self.dir / "dead2.py").write_text(
            "def e():\n    return 1\n", encoding="utf-8")
        self.measure()
        r = run_checker("40", cwd=self.dir)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("dead2.py", r.stderr)
        self.assertIn("0.0%", r.stderr)


class EdgeCases(Base):
    def test_zero_statement_files_do_not_fail_the_floor(self):
        # pkg/__init__.py has zero statements and must not be listed even
        # when another file fails: a zero denominator must read as "skip",
        # not as an error or as a failing percentage.
        r = run_checker("55", cwd=self.dir)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertNotIn("__init__", r.stderr)

    def test_missing_data_is_an_error(self):
        (self.dir / ".coverage").unlink()
        r = run_checker("40", cwd=self.dir)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("cannot read coverage data", r.stderr)

    def test_data_file_flag_routes_the_read(self):
        # Run from a second empty tempdir (no .coverage of its own): only the
        # explicit --data-file can find the data.
        with tempfile.TemporaryDirectory() as td2:
            r = run_checker("--data-file", str(self.dir / ".coverage"), cwd=Path(td2))
            self.assertEqual(r.returncode, 0, r.stderr)
            # The success line names the lowest file: it must come from the
            # tempdir's data, proving --data-file routed the read.
            self.assertIn(str(self.dir / "dead.py"), r.stdout)

    def test_rcfile_flag_routes_the_data_file(self):
        # A config whose data_file points into the tempdir must be honoured
        # even when the checker runs from an unrelated cwd.
        rc = self.dir / "rc"
        rc.write_text(f"[run]\ndata_file = {self.dir / '.coverage'}\n",
                      encoding="utf-8")
        with tempfile.TemporaryDirectory() as td2:
            r = run_checker("--rcfile", str(rc), cwd=Path(td2))
            self.assertEqual(r.returncode, 0, r.stderr)
            # The success line names the lowest file: it must come from the
            # tempdir's data, proving the rcfile's data_file routed the read.
            self.assertIn(str(self.dir / "dead.py"), r.stdout)


class WithoutCoverage(unittest.TestCase):
    """The no-coverage path: ci-local.sh's fallback runs the suite without the
    gate when coverage is missing, and the checker must fail loudly (not pass
    vacuously) so a missing dependency can never read as "floor satisfied"."""

    def test_missing_coverage_fails_loudly(self):
        # Under `coverage run` the parent's subprocess patch preloads coverage
        # into the child at site-init (before any -c code runs), so evicting
        # the cached module is as important as blocking the import finder.
        code = ("import sys\n"
                "for m in [m for m in sys.modules if m == 'coverage' or m.startswith('coverage.')]:\n"
                "    del sys.modules[m]\n"
                "class Block:\n"
                "    def find_spec(self, name, path=None, target=None):\n"
                "        if name == 'coverage' or name.startswith('coverage.'):\n"
                "            raise ImportError('blocked')\n"
                "sys.meta_path.insert(0, Block())\n"
                "sys.path.insert(0, %r)\n"
                "import check_coverage_floor as ccf\n"
                "rc = ccf.main(['40'])\n"
                "print('RC', rc)\n"
                % str(REPO / "scripts"))
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True)
        self.assertIn("RC 1", r.stdout, r.stderr)
        self.assertIn("coverage is not installed", r.stderr)


class OnlyZeroStatementFiles(unittest.TestCase):
    """A data file holding only zero-statement files (empty __init__.py) must
    pass: nothing measurable is below the floor."""

    def test_zero_statement_only_report_passes(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "pkg").mkdir()
            (d / "pkg" / "__init__.py").write_text("", encoding="utf-8")
            (d / "runner.py").write_text(
                "import sys; sys.path.insert(0, '.')\nimport pkg\n",
                encoding="utf-8")
            # --include (not --source) so the report holds only pkg/: the
            # runner itself stays out and every measured file has 0 statements.
            r = subprocess.run(
                [sys.executable, "-m", "coverage", "run",
                 "--include", "*/pkg/*", "runner.py"],
                capture_output=True, text=True, cwd=str(d))
            self.assertEqual(r.returncode, 0, r.stderr)
            r2 = run_checker("40", cwd=d)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertIn("none with statements", r2.stdout)


if __name__ == "__main__":
    unittest.main()
