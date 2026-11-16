"""The scrubbed-env builders must keep their children measurable.

Issue #14's class: a test module builds a child-process env from scratch
(fake HOME, a handful of keys) and the dict silently drops the COVERAGE_*
variables that `.coveragerc`'s `patch = subprocess` injected into the
parent. Every line such a child executes is then invisible to the coverage
gate while the tests stay green, and in an isolated run of that module
alone the file reads far below its full-suite number (supervisor.py read
82% when `tests.test_runtime_recovery` + `tests.test_runtime_cli_handlers`
ran alone: 40 missed statements in supervise(), reserve() and
main_supervise(), which those modules' own children execute).

PR #12 fixed the first two sites and PR #15 the next two by forwarding
`COVERAGE_*` through the builder. The two modules here were the remaining
ones (flagged by PR #73's review): both spawn the shipped supervisor as a
real child — `launch()`/`running()` in recovery, the `LaunchCommand`
success path in cli_handlers — so their measurement loss is reproducible
without any other module running.

These tests pin the contract on both halves:

- the env dict itself carries every COVERAGE_* key when the suite runs
  under `coverage run`, and stays as scrubbed as before when it does not;
- a child started with the module's own env builder is actually measured
  (end-to-end: the child writes a coverage data file) — this is the half
  that would go quietly stale if the forwarding were dropped again, since
  the full suite's other children mask the loss and only an isolated run
  shows it.

Skipped entirely when the suite is not run under coverage (plain
`unittest discover`): there is nothing to forward then, and the inertness
assertions live in the first test instead.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# A python child that exits 0. NOT under .coveragerc's source dirs, so what
# a traced child records for it is irrelevant — the test asserts the data
# file's EXISTENCE, which is the part that breaks when the env builder
# drops the tracing keys.
CHILD = HERE / "_cov_child_probe.py"


def _under_coverage() -> bool:
    return bool(os.environ.get("COVERAGE_PROCESS_START")
                or os.environ.get("COVERAGE_PROCESS_CONFIG"))


def _make_base(module_name: str):
    """Instantiate the target module's Base: setUp + its cleanups, nothing else.

    unittest.TestCase allows manual setUp() on an un-run instance; calling
    doCleanups() afterwards runs the stack setUp registered (the fake
    adapter's restore, the temp dir removal) exactly as a real run would.
    """
    import importlib
    mod = importlib.import_module(module_name)
    base = mod.Base("setUp")           # never run; we only borrow setUp
    base.setUp()
    return base, base.doCleanups


class ScrubbedEnvForwardsCoverageKeys(unittest.TestCase):
    """The two builders' env dicts carry the tracing keys."""

    def _env_of(self, module_name: str):
        base, cleanup = _make_base(module_name)
        try:
            return dict(base.env)
        finally:
            cleanup()

    def test_recovery_env_forwards_coverage_keys(self):
        if not _under_coverage():
            self.skipTest("suite not running under coverage")
        env = self._env_of("tests.test_runtime_recovery")
        for k, v in os.environ.items():
            if k.startswith("COVERAGE_"):
                self.assertEqual(env.get(k), v,
                                 f"{k} dropped by test_runtime_recovery's env builder")

    def test_cli_handlers_env_forwards_coverage_keys(self):
        if not _under_coverage():
            self.skipTest("suite not running under coverage")
        env = self._env_of("tests.test_runtime_cli_handlers")
        for k, v in os.environ.items():
            if k.startswith("COVERAGE_"):
                self.assertEqual(env.get(k), v,
                                 f"{k} dropped by test_runtime_cli_handlers's env builder")

    def test_scrubbing_is_unchanged_outside_coverage(self):
        # The forwarding must not un-scrub the env when the suite is not
        # measured: the isolation contract (fake HOME, none of the parent's
        # ambient keys) is the point of these builders, and a plain
        # `os.environ` copy would smuggle everything in. With the tracing
        # keys absent the builder must produce exactly its own keys: no
        # COVERAGE_* at all and HOME still the faked temp dir.
        # (`COVERAGE_RUN` — set by `coverage run` for its own process — is
        # part of the same prefix pass-through and carries no config, so
        # its presence or absence is not asserted here either way.)
        saved = {k: os.environ.get(k) for k in os.environ
                 if k.startswith("COVERAGE_")}
        try:
            for k in saved:
                os.environ.pop(k, None)
            for module_name in ("tests.test_runtime_recovery",
                                "tests.test_runtime_cli_handlers"):
                with self.subTest(module=module_name):
                    base, cleanup = _make_base(module_name)
                    try:
                        env = dict(base.env)
                        home = base.tmp
                    finally:
                        cleanup()
                    self.assertFalse([k for k in env
                                      if k.startswith("COVERAGE_")],
                                      f"{module_name} forwards COVERAGE_* "
                                      "unconditionally")
                    self.assertEqual(env["HOME"], str(home))
                    # And the env is still a hand-built dict, not a copy of
                    # the parent's: an ambient key the builder never names
                    # must stay out.
                    ambient = next((k for k in os.environ
                                    if k not in env and k != "PATH"), None)
                    if ambient is not None:
                        self.assertNotIn(ambient, env)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_children_spawned_with_the_builders_env_are_measured(self):
        # End-to-end half: start a python child with exactly the env dict
        # the module's own builder produces and require it to record a
        # coverage data file. This is what silently stopped working for the
        # two modules (their supervisor children ran unmeasured on every
        # layout, hidden only because other modules' children traced the
        # same lines in the full suite).
        #
        # Two adjustments keep the assertion about the CONTRACT (the env
        # dict carries what a child needs to be measured), not about where
        # coverage happens to be installed:
        #
        # - COVERAGE_PROCESS_CONFIG (set by `.coveragerc`'s `patch =
        #   subprocess`) names the PARENT's data file and takes precedence
        #   over COVERAGE_PROCESS_START in the child's startup hook, so the
        #   child's data would land in the repo dir, not this test's temp
        #   dir. It is dropped only after the first test proved the builder
        #   forwards it, and START is pointed at a probe rcfile whose
        #   data_file lives under the temp dir.
        # - the child's PYTHONPATH names the parent's own site-packages so
        #   the probe script can import coverage at all under its scrubbed
        #   HOME. That HOME-vs-install-location coupling is a separate
        #   finding (held PR #73's covsite work); the probe imports
        #   coverage itself and calls process_startup(), which is what the
        #   a1_coverage.pth hook does for a child that can find it.
        # Skip BEFORE importing coverage: ci-local.sh's job_unit explicitly
        # supports the suite running without coverage installed ("fall back
        # to a bare run"), and in that mode this module is imported with no
        # COVERAGE_* in the env — the import below would report
        # ModuleNotFoundError as an ERROR instead of a skip (same guard
        # shape as tests/test_check_coverage_floor.py's HAVE_COVERAGE).
        if not _under_coverage():
            self.skipTest("suite not running under coverage")
        import coverage  # the suite is measured, so this import exists
        site_dir = os.path.dirname(os.path.dirname(coverage.__file__))
        for module_name in ("tests.test_runtime_recovery",
                            "tests.test_runtime_cli_handlers"):
            with self.subTest(module=module_name):
                base, cleanup = _make_base(module_name)
                try:
                    env = dict(base.env)
                finally:
                    cleanup()
                self.assertIn("COVERAGE_PROCESS_START", env,
                              f"{module_name}'s env builder dropped "
                              "COVERAGE_PROCESS_START; nothing to measure "
                              "the child with")
                with tempfile.TemporaryDirectory() as td:
                    probe_rc = Path(td) / "covrc"
                    probe_rc.write_text("[run]\ndata_file = "
                                        f"{td}/.coverage\nparallel = True\n",
                                        encoding="utf-8")
                    child_env = dict(env)
                    child_env.pop("COVERAGE_PROCESS_CONFIG", None)
                    child_env["COVERAGE_PROCESS_START"] = str(probe_rc)
                    child_env["PYTHONPATH"] = site_dir
                    r = subprocess.run([sys.executable, str(CHILD)], cwd=td,
                                       env=child_env, capture_output=True,
                                       text=True, timeout=60)
                    self.assertEqual(r.returncode, 0, r.stderr)
                    data = [f for f in os.listdir(td)
                            if f.startswith(".coverage.")]
                    self.assertTrue(
                        data, f"a child started with {module_name}'s env "
                              "builder wrote no coverage data file; the "
                              "scrubbed env defeats subprocess measurement "
                              "again (issue #14 class)")


if __name__ == "__main__":
    unittest.main()
