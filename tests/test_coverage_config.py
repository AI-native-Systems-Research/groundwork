"""Tests for .coveragerc — the measurement contract CI and ci-local.sh share.

CI's `unit` job and scripts/ci-local.sh's job_unit both measure through
.coveragerc (COVERAGE_PROCESS_START → this file), so every key in it changes
what both gates see. These tests pin that contract:

  * branch collection is on — a guard whose other outcome never runs still
    counts its body as covered by statement alone (at main 8d40c69 the suite
    read 94% statement but 89% branch: 273 of 2548 arcs never taken, including
    five in doctor.py while it read 100% by statement);
  * the source scope covers both Python roots (issue #10: a file outside
    `source` is invisible to every floor);
  * the subprocess/_exit patches and `parallel` are what make env-scrubbed
    children measurable at all (the suite under-reports at 68% without them);
  * the total floor stays at 80 so the per-file floor
    (scripts/check_coverage_floor.py) remains the regression catcher for a
    single file dropping to 0%.

The branch test also proves the setting reaches the gate's input end to end:
a branch-enabled measurement of a file whose statements are fully covered
but whose `if` never goes false must read below 100% through
check_coverage_floor's own reading of the data.

Run from the repo root:  python3 -m unittest tests.test_coverage_config -v
"""
import configparser
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RC = REPO / ".coveragerc"

sys.path.insert(0, str(REPO / "scripts"))
import check_coverage_floor as ccf  # noqa: E402

try:
    import coverage  # noqa: F401
    HAVE_COVERAGE = True
except ImportError:
    HAVE_COVERAGE = False


def rc_config() -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    # configparser.read() returns the paths it parsed as *strings* (a Path
    # object never compares equal to them), so compare the string form.
    read = cp.read(RC, encoding="utf-8")
    assert str(RC) in read, f"{RC} is missing or unreadable"
    return cp


class SharedMeasurementContract(unittest.TestCase):
    """What every gate reading .coveragerc may rely on."""

    def setUp(self):
        self.cp = rc_config()

    def test_branch_collection_is_enabled(self):
        # Statement-only coverage counts a guard's body as covered even when
        # its other outcome never runs. Without `branch = true` the metric
        # cannot see that class at all (doctor.py read 100% by statement
        # with five never-taken arcs at main 8d40c69).
        self.assertTrue(self.cp.getboolean("run", "branch", fallback=False),
                        ".coveragerc [run] must set branch = true")

    def test_source_scope_covers_both_python_roots(self):
        # Issue #10: a file outside `source` is invisible to every floor —
        # check_skill_spec.py sat at 0% unnoticed for exactly that reason.
        source = [s.strip() for s in
                  self.cp.get("run", "source", fallback="").splitlines() if s.strip()]
        self.assertIn("skills/investigation/scripts", source)
        self.assertIn("scripts", source)

    def test_subprocess_tracing_patches_are_present(self):
        # Env-scrubbed children carry the suite's subprocess-heavy files:
        # without these patches the suite under-reports at 68% (README of
        # this config) and new_study.py reads 0%.
        patch = [p.strip() for p in
                 self.cp.get("run", "patch", fallback="").splitlines() if p.strip()]
        self.assertIn("subprocess", patch)
        self.assertIn("_exit", patch)
        self.assertTrue(self.cp.getboolean("run", "parallel", fallback=False))

    def test_total_floor_stays_a_total_only_gate(self):
        # The per-file floor (scripts/check_coverage_floor.py) is the catcher
        # for one file dropping to 0%; the total floor must stay total-only
        # and not silently drift as the metric changes shape.
        self.assertEqual(self.cp.getint("report", "fail_under", fallback=0), 80)


@unittest.skipUnless(HAVE_COVERAGE, "branch data needs coverage installed")
class BranchDataReachesTheGate(unittest.TestCase):
    """`branch = true` is not just a declaration: the gate's own reader must
    see never-taken arcs in a file whose statements are fully covered."""

    def test_fully_covered_statements_with_a_dead_branch_read_below_100(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            # guarded.py: every statement executes, but the loop always leaves
            # through `break` — the loop-exhaustion arc (for -> return) never
            # runs. Statement-only data calls the file 100%; branch data must
            # not. This is the exact shape that hid five never-taken arcs in
            # doctor.py while it read 100% by statement at main 8d40c69.
            (d / "guarded.py").write_text(
                "def f(xs):\n"
                "    total = 0\n"
                "    for x in xs:\n"
                "        total += x\n"
                "        if total > 10:\n"
                "            break\n"
                "    return total\n", encoding="utf-8")
            (d / "runner.py").write_text(
                "import sys\nsys.path.insert(0, '.')\nimport guarded\n"
                "assert guarded.f([5, 20]) == 25\n", encoding="utf-8")
            # Measure the way the repo's own rc does: branch collection on
            # (mirrors [run] branch = true), source = the tempdir.
            rc = d / "rc"
            rc.write_text("[run]\nbranch = true\nsource =\n    .\n"
                          f"data_file = {d / '.coverage'}\n", encoding="utf-8")
            r = subprocess.run(
                [sys.executable, "-m", "coverage", "run", "--rcfile", str(rc),
                 "runner.py"],
                capture_output=True, text=True, cwd=str(d))
            self.assertEqual(r.returncode, 0, r.stderr)
            # And a statement-only twin, to prove the difference is the branch
            # data and not the scenario.
            rc_stmt = d / "rc_stmt"
            rc_stmt.write_text("[run]\nsource =\n    .\n"
                               f"data_file = {d / '.coverage.stmt'}\n",
                               encoding="utf-8")
            r2 = subprocess.run(
                [sys.executable, "-m", "coverage", "run", "--rcfile", str(rc_stmt),
                 "runner.py"],
                capture_output=True, text=True, cwd=str(d))
            self.assertEqual(r2.returncode, 0, r2.stderr)

            import json as _json
            from coverage import Coverage

            for data_file, expect_lt_100 in ((d / ".coverage", True),
                                             (d / ".coverage.stmt", False)):
                cov = Coverage(data_file=str(data_file))
                cov.load()
                out = d / "report.json"
                cov.json_report(outfile=str(out))
                rep = _json.loads(out.read_text(encoding="utf-8"))
                g = rep["files"][str(d / "guarded.py")]["summary"]
                if expect_lt_100:
                    self.assertEqual(g["num_statements"], g["covered_lines"],
                                     "every statement runs; only the loop-exit "
                                     "arc is dead")
                    self.assertLess(g["percent_covered"], 100.0,
                                    "branch data must see the never-taken "
                                    "loop-exhaustion arc")
                else:
                    self.assertEqual(g["percent_covered"], 100.0,
                                     "the statement-only twin reads 100% — "
                                     "the difference is the branch data")


if __name__ == "__main__":
    unittest.main()
