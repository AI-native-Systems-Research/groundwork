"""Tests for scripts/ci-local.sh's unit job: the total coverage floor must
fail the job, not just print a failure the subshell swallows.

job_unit runs its coverage sequence inside a `( ... )` subshell under
`set -u` only — there is no `set -e`, so a command that fails on a
non-final line is discarded and the subshell's exit status is whatever
the LAST command returns. PR #13 appended the per-file floor check
(scripts/check_coverage_floor.py) after `coverage report`, and that gave
the unguarded report line exactly that shape:

    coverage report          # exits 2 when TOTAL < fail_under=80
    check_coverage_floor.py  # exits 0 when every file >= 40%

A repo whose total dropped below 80 but whose every file stayed above the
40% per-file floor printed "Coverage failure: total of N is less than
fail-under=80" and the unit job still reported PASS — data the same
runner failed on before PR #13's floor step existed. CI itself never had
the bug (Actions runs bash -e), so only the local runner — the one
documented as "run the same four jobs locally" — lied.

These tests pin the guard two ways:

  * behaviourally: the exact job_unit subshell shape is replayed against a
    synthetic repo whose data holds TOTAL 75% (below fail_under=80) with
    every file at 75% (above the 40% per-file floor) — the regression's
    own scenario — and the subshell must exit nonzero.
  * textually: each gate line of the subshell (run, combine, report) must
    carry its own `|| exit 1`, matching the style the fix brought the
    report line into, so a future step appended after a failing one
    cannot resurrect the swallowed-exit class.

Run from the repo root:  python3 -m unittest tests.test_ci_local_total_floor -v
"""
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOCAL = REPO / "scripts" / "ci-local.sh"

try:
    import coverage  # noqa: F401
    HAVE_COVERAGE = True
except ImportError:
    HAVE_COVERAGE = False

# The gate lines of job_unit's subshell, in order. Each one's failure must
# stop the subshell; only the final floor check may rely on being last.
GATE_LINES = (
    '"$PY" -m coverage run -m unittest discover tests || exit 1',
    '"$PY" -m coverage combine || exit 1',
    '"$PY" -m coverage report || exit 1',
)


def local_text() -> str:
    return LOCAL.read_text()


def unit_body(text: str) -> str:
    """job_unit's body, from the function header to its closing brace."""
    m = re.search(r"^job_unit\(\) \{.*?^\}", text, re.M | re.S)
    assert m, "job_unit() not found in ci-local.sh"
    return m.group(0)


def unit_subshell_body(text: str) -> str:
    """The `( ... )` block of job_unit: the part whose exit status is the
    job's verdict. The subshell opens on its own line (two-space indent)
    and closes with an indented `)` at line start."""
    body = unit_body(text)
    i = body.find("\n  (\n")
    assert i >= 0, "job_unit's subshell opening not found"
    j = body.find("\n  )", i)
    assert j >= 0, "job_unit's subshell closing not found"
    return body[i + 1:j + 4]


@unittest.skipUnless(HAVE_COVERAGE,
                     "the scenario needs coverage installed")
class TotalFloorFailsTheJob(unittest.TestCase):
    """Replay job_unit's exact subshell shape on a synthetic repo whose
    coverage data is below the total floor but above the per-file floor,
    and require the job to fail. Regresses the swallowed exit of the
    unguarded `coverage report` line (run via bash, like ci-local.sh)."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.dir = Path(self._td.name)
        # The same [run]/[report] shape as the repo's .coveragerc:
        # source-scoped, parallel, total fail_under=80.
        (self.dir / ".coveragerc").write_text(
            "[run]\nsource =\n    pkg\nparallel = True\n\n"
            "[report]\nfail_under = 80\n", encoding="utf-8")
        (self.dir / "pkg").mkdir()
        # pkg/mod.py: 12 statements (6 defs + 6 returns), 9 executed on
        # import + the three covered calls = 75% — above the 40% per-file
        # floor, below the 80% total floor.
        (self.dir / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (self.dir / "pkg" / "mod.py").write_text(
            "def a():\n    return 1\n"
            "def b():\n    return 2\n"
            "def c():\n    return 3\n"
            "def u1():\n    return 4\n"
            "def u2():\n    return 5\n"
            "def u3():\n    return 6\n", encoding="utf-8")
        (self.dir / "test_pkg.py").write_text(
            "import unittest\nfrom pkg import mod\n\n"
            "class T(unittest.TestCase):\n"
            "    def test_runs(self):\n"
            "        self.assertEqual(mod.a() + mod.b() + mod.c(), 6)\n",
            encoding="utf-8")
        # The real script invokes the floor checker by absolute path
        # ($REPO/scripts/...); copy it in so the sandbox replays that.
        (self.dir / "scripts").mkdir()
        (self.dir / "scripts" / "check_coverage_floor.py").write_text(
            (REPO / "scripts" / "check_coverage_floor.py").read_text(),
            encoding="utf-8")

    def run_unit_subshell(self, guard_report: bool) -> subprocess.CompletedProcess:
        """Run job_unit's subshell shape via bash with the repo's exact
        line sequence; guard_report toggles only the `|| exit 1` after
        `coverage report`, the one line the fix added."""
        report = ('"$PY" -m coverage report || exit 1' if guard_report
                  else '"$PY" -m coverage report')
        script = (
            "set -u\n"
            'PY="${PYTHON:-python3}"\n'
            'REPO="$(pwd)"\n'
            "(\n"
            '  cd "$REPO" || exit 1\n'
            '  export COVERAGE_PROCESS_START="$REPO/.coveragerc"\n'
            '  "$PY" -m coverage erase\n'
            '  "$PY" -m coverage run -m unittest discover . || exit 1\n'
            '  "$PY" -m coverage combine || exit 1\n'
            f"  {report}\n"
            '  "$PY" "$REPO/scripts/check_coverage_floor.py"\n'
            ")\n")
        return subprocess.run(["bash", "-c", script], cwd=str(self.dir),
                              capture_output=True, text=True)

    def test_total_below_floor_fails_the_unit_job(self):
        # The fix: with the guard, the subshell's status is the report's
        # exit 2, not the floor check's exit 0.
        r = self.run_unit_subshell(guard_report=True)
        self.assertNotEqual(r.returncode, 0,
                            f"total-floor failure swallowed: {r.stdout}")
        self.assertIn("less than fail-under=80", r.stdout + r.stderr)

    def test_unguarded_report_swallows_the_failure(self):
        # The control that proves the scenario is the regression's own:
        # without `|| exit 1` the same data PASSes — the report fails
        # (exit 2), the floor check passes (every file 75% >= 40%), and
        # the subshell returns the floor check's 0.
        r = self.run_unit_subshell(guard_report=False)
        self.assertEqual(r.returncode, 0,
                         f"scenario drifted: {r.stdout}{r.stderr}")
        self.assertIn("less than fail-under=80", r.stdout + r.stderr)
        self.assertIn("OK per-file floor", r.stdout + r.stderr)


class UnitSubshellGuards(unittest.TestCase):
    """Textual pin on job_unit's subshell: each gate line guards its own
    exit, and the per-file floor check stays last (its status is the
    subshell's, so it needs no guard of its own)."""

    def test_every_gate_line_guards_its_own_exit(self):
        # run/combine/report each fail the subshell when they fail; the
        # report line is the one the regression left unguarded.
        body = unit_subshell_body(local_text())
        for line in GATE_LINES:
            self.assertIn(line, body, f"gate line lost its guard: {line!r}")

    def test_report_line_is_guarded(self):
        # The regression itself: `coverage report` exits 2 under fail_under
        # and is not the subshell's last line, so it must guard itself.
        self.assertIn('"$PY" -m coverage report || exit 1',
                      unit_subshell_body(local_text()))

    def test_floor_check_is_the_last_command(self):
        # The floor check needs no `|| exit 1`: it is the subshell's final
        # command, so its own exit status IS the subshell's. If it stops
        # being last, it must grow a guard (and this test must change).
        body = unit_subshell_body(local_text())
        cmds = [ln.strip() for ln in body.splitlines()
                if ln.strip().startswith('"$PY"')]
        self.assertTrue(cmds, "job_unit's subshell lost its commands")
        self.assertIn("check_coverage_floor.py", cmds[-1])
        # And the guard must not have become "stop before the floor": the
        # per-file floor (issues #2/#3/#8) still runs after the report.
        self.assertLess(body.index("coverage report"),
                        body.index("check_coverage_floor.py"))


if __name__ == "__main__":
    unittest.main()
