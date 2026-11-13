"""Tests for .github/workflows/ci.yml — what the workflow claims and runs.

The workflow header is documentation read by every contributor, and issue
#70 showed it drifting: the file opened with "# INACTIVE until the repo
is published" long after the repo went public (2026-11-10) and CI began
running on every push and pull request. That staleness class is what
these tests pin, from the workflow side:

1. The header's standing promise — the same jobs run locally via
   scripts/ci-local.sh — is enforced as structure: the trigger set
   (`on:`), the job ids, and the gates each job runs are pinned from the
   parsed YAML and cross-checked against ci-local.sh's job functions, so
   a doc-shaped edit to the workflow cannot silently change what the
   workflow does, and a gate added on one side without the other is
   visible the moment it lands on the side that is checked here.
2. Known parity gaps that require editing .github/workflows/ci.yml are
   tracked as needs-human items on issue #70 (this agent's token cannot
   push workflow files); test_local_gates documents them by asserting
   the local side runs the gates CI currently lacks.

Run from the repo root:  python3 -m unittest tests.test_ci_workflow_docs -v
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CI = REPO / ".github" / "workflows" / "ci.yml"
LOCAL = REPO / "scripts" / "ci-local.sh"

try:
    import yaml
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False

# The workflow's four jobs, spelled as ci-local.sh's default JOBS array
# spells them (issue #70's replacement header names them the same way).
CI_JOBS = ("unit", "spec", "install-smoke", "no-hardcoded-paths")


def ci_text() -> str:
    return CI.read_text()


def local_text() -> str:
    return LOCAL.read_text()


def header(text: str) -> str:
    """The leading comment block, before the first non-comment line."""
    lines = []
    for line in text.splitlines():
        if line.startswith("#"):
            lines.append(line)
        else:
            break
    return "\n".join(lines)


def job_bodies(text: str) -> dict:
    """Each job_() function body of ci-local.sh, keyed by job name."""
    starts = [(m.group(1), m.start()) for m in
              re.finditer(r"^job_([a-z_]+)\(\) \{", text, re.M)]
    starts.append(("_end", len(text)))
    return {name: text[a:b] for (name, a), (_, b) in zip(starts, starts[1:])}


class WorkflowDocs(unittest.TestCase):
    """The workflow file's own prose must match what the workflow does."""

    def test_header_names_the_local_runner_for_the_same_jobs(self):
        # The header's standing promise: the same jobs run locally. Both
        # halves must stay true together — ci-local.sh must be named, and
        # the job parity must hold (checked in LocalCIParity below).
        # The header's stale "# INACTIVE" banner is deliberately not
        # asserted here: the fix edits .github/workflows/ci.yml, which
        # needs the Workflows permission and is tracked as needs-human on
        # issue #70 with verbatim replacement text.
        self.assertIn("scripts/ci-local.sh", header(ci_text()))


@unittest.skipUnless(HAVE_YAML, "PyYAML not installed (CI's unit matrix "
                                "includes 3.10 hosts where it may be absent)")
class WorkflowStructure(unittest.TestCase):
    """What the workflow does, from the parsed YAML."""

    @classmethod
    def setUpClass(cls):
        cls.doc = yaml.safe_load(ci_text())
        # YAML 1.1 parses a bare `on:` key as boolean True.
        cls.on = cls.doc.get(True) or cls.doc.get("on")
        cls.jobs = cls.doc["jobs"]

    def test_runs_on_push_to_main_and_every_pull_request(self):
        self.assertEqual(self.on,
                         {"push": {"branches": ["main"]}, "pull_request": None})

    def test_job_ids_match_ci_local_jobs(self):
        self.assertEqual(set(self.jobs), set(CI_JOBS))
        # And the local script really offers exactly those jobs, in its
        # usage line and its default JOBS array.
        declared = re.search(r"^#\s+jobs:\s+(.*?)\s+\(default", local_text(), re.M)
        self.assertTrue(declared, "ci-local.sh usage line stopped naming jobs")
        self.assertEqual(declared.group(1).split(), list(CI_JOBS))
        defaults = re.search(r"&& JOBS=\(([^)]*)\)", local_text())
        self.assertTrue(defaults, "ci-local.sh lost its default JOBS array")
        self.assertEqual(defaults.group(1).split(), list(CI_JOBS))

    def test_every_ci_job_runs_on_ubuntu_latest(self):
        for jid, job in self.jobs.items():
            with self.subTest(job=jid):
                self.assertEqual(job["runs-on"], "ubuntu-latest")

    def test_unit_matrix_covers_python_310_and_312(self):
        matrix = self.jobs["unit"]["strategy"]["matrix"]["python-version"]
        self.assertEqual(matrix, ["3.10", "3.12"])
        self.assertFalse(self.jobs["unit"]["strategy"]["fail-fast"])


class LocalCIParity(unittest.TestCase):
    """ci-local.sh is documented as running "the same jobs" as the
    workflow; each side's gates are pinned so a gate added on one side
    without the other is caught the moment the checked side changes.
    Gaps that live in the workflow file itself are needs-human (issue
    #70: the CI unit job lacks ci-local.sh's per-file coverage floor
    step; test_local_gates pins the local side so the gap stays named).
    """

    def setUp(self):
        self.ci = ci_text()
        self.local = local_text()
        self.bodies = job_bodies(self.local)

    def test_local_gates(self):
        # What scripts/ci-local.sh's jobs actually run today — including
        # the per-file coverage floor, which the CI unit job does not run
        # yet (issue #70, needs-human: workflows permission). If this
        # fails, the local runner changed and the parity picture in this
        # file (and issue #70) needs a refresh.
        self.assertIn("check_coverage_floor.py", self.bodies["unit"])
        for job in CI_JOBS:
            self.assertIn(job.replace("-", "_"), self.bodies,
                          f"ci-local.sh lost job_{job.replace('-', '_')}")

    def test_unit_job_runs_the_same_coverage_sequence(self):
        # The unit job's core is the run/combine/report sequence under
        # coverage, identical in both files, in that order: combining
        # before running, or reporting before combining, produces no
        # usable gate.
        steps = self.workflow_unit_steps()
        commands = ("coverage run -m unittest discover tests",
                    "coverage combine",
                    "coverage report")
        positions = []
        for command in commands:
            self.assertIn(command, steps, command)
            positions.append(steps.find(command))
        self.assertEqual(positions, sorted(positions),
                         "unit job's coverage steps reordered: "
                         f"{commands} vs step order {positions}")
        self.assertIn("coverage combine", self.bodies["unit"])
        self.assertIn("coverage report", self.bodies["unit"])

    def workflow_unit_steps(self) -> str:
        if not HAVE_YAML:
            self.skipTest("PyYAML not installed")
        steps = yaml.safe_load(ci_text())["jobs"]["unit"]["steps"]
        return "\n".join(s.get("run", "") for s in steps)

    def test_coverage_config_is_shared_with_the_local_runner(self):
        # The unit job measures through .coveragerc (source, subprocess
        # tracing, fail_under) so both sides gate on the same numbers.
        self.assertIn(".coveragerc", self.ci)
        self.assertIn(".coveragerc", self.bodies["unit"])

    def test_spec_job_runs_the_same_two_gates(self):
        # spec = the reference validator + scripts/check_skill_spec.py.
        for fragment in ("validate", "check_skill_spec.py"):
            self.assertIn(fragment, self.ci)
            self.assertIn(fragment, self.bodies["spec"])

    def test_install_smoke_job_runs_the_same_smoke(self):
        # The R9 install smoke (tests/_runtime_install_smoke.py) runs in
        # both files; test_runtime_acceptance.InstallSmoke pins the exact
        # invocation, this pins that each side runs it.
        for fragment in ("skills add", "_runtime_install_smoke.py"):
            self.assertIn(fragment, self.ci)
            self.assertIn(fragment, self.bodies["install_smoke"])

    def test_no_hardcoded_paths_job_matches(self):
        self.assertIn("/Users/", self.ci)
        self.assertIn("/Users/", self.bodies["no_hardcoded_paths"])


if __name__ == "__main__":
    unittest.main()
