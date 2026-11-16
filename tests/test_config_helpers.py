"""_config.py: the failure faces the indirect suites never reach.

Every script imports _config (doctor, collect, capture_prom, new_study,
verify_upstreams, the runtime adapters), but the existing suites only ever
exercise its happy paths, and only indirectly — doctor's subprocess tests run
the child with a parseable config or none at all, and test_verify_upstreams.py
drives load_yaml's bad-YAML face through main(). Measured on main 8d40c69
(CI run 43504194546, unit 3.12; reproduced locally with subprocess tracing)
the module reads 87% with exactly 10 statements never reached:

  - yaml_available's ImportError face (38-39) — Nous reports it as the
    pyyaml tool line; nothing ever calls it with PyYAML missing
  - config_home's XDG_CONFIG_HOME fallback (60)
  - _load_mapping's empty-document (73) and non-mapping (74-75) branches
  - git()'s OSError/TimeoutExpired face (90-91)
  - runs_root's load-machine-config recovery (133-136): an unparseable
    machine config degrades to DEFAULT_RUNS instead of crashing

These tests import _config in-process and drive each face directly: no
subprocess, no environment scrubbing, so every line counts toward coverage.
Temp $HOME / $INVESTIGATION_CONFIG_HOME / $XDG_CONFIG_HOME per test, the
mock.patch.dict pattern test_doctor_inprocess.py's InProcessEnv uses.
"""
import builtins
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills/investigation/scripts"

sys.path.insert(0, str(SCRIPTS))
import _config  # noqa: E402


class IsolatedEnv(unittest.TestCase):
    """A temp HOME / INVESTIGATION_CONFIG_HOME per test, nothing leaked."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.cfg = self.tmp / "cfg"
        self.cfg.mkdir()

    def env(self, **extra):
        # XDG_CONFIG_HOME, INVESTIGATION_CONFIG_HOME and INVESTIGATION_RUNS
        # are stripped, not merged: mock.patch.dict merges without clearing,
        # so an ambient value would survive and beat the branch the test
        # asserts (GitHub runners export XDG_CONFIG_HOME=/home/runner/.config;
        # the other two are the same hazard with no known runner that trips
        # them). The empty string is falsy, so _config treats each var as
        # unset and falls through to its default. Tests that exercise one of
        # these branches set it back explicitly.
        base = {"HOME": str(self.home),
                "INVESTIGATION_CONFIG_HOME": str(self.cfg),
                "INVESTIGATION_RUNS": "",
                "XDG_CONFIG_HOME": ""}
        base.update(extra)
        return base


class TestYamlAvailable(IsolatedEnv):
    def test_import_error_face_returns_false(self):
        # The face Nous's pyyaml tool line consumes: PyYAML missing must
        # report False, not raise, so stdlib-only callers keep working.
        real_import = builtins.__import__

        def block_yaml(name, *a, **k):
            if name == "yaml" or name.startswith("yaml."):
                raise ImportError("blocked")
            return real_import(name, *a, **k)

        with mock.patch.dict(os.environ, self.env()):
            builtins.__import__ = block_yaml
            try:
                self.assertFalse(_config.yaml_available())
            finally:
                builtins.__import__ = real_import

    def test_load_yaml_raises_config_error_when_pyyaml_is_missing(self):
        # The sibling face (load_yaml's ImportError) is folded in here: the
        # contract is ConfigError with the install hint, never a bare
        # ImportError escaping to the caller.
        real_import = builtins.__import__

        def block_yaml(name, *a, **k):
            if name == "yaml" or name.startswith("yaml."):
                raise ImportError("blocked")
            return real_import(name, *a, **k)

        with mock.patch.dict(os.environ, self.env()):
            builtins.__import__ = block_yaml
            try:
                with self.assertRaises(_config.ConfigError) as cm:
                    _config.load_yaml(self.cfg / "config.yaml")
                self.assertIn("PyYAML is not installed", str(cm.exception))
            finally:
                builtins.__import__ = real_import


class TestConfigHome(IsolatedEnv):
    def test_xdg_fallback_without_investigation_home(self):
        # No INVESTIGATION_CONFIG_HOME: the home is XDG_CONFIG_HOME/investigation.
        xdg = self.tmp / "xdg"
        with mock.patch.dict(os.environ, {**self.env(),
                                          "INVESTIGATION_CONFIG_HOME": "",
                                          "XDG_CONFIG_HOME": str(xdg)}):
            self.assertEqual(_config.config_home(), xdg / "investigation")

    def test_investigation_home_wins_over_xdg(self):
        xdg = self.tmp / "xdg"
        with mock.patch.dict(os.environ,
                             {**self.env(), "XDG_CONFIG_HOME": str(xdg)}):
            self.assertEqual(_config.config_home(), self.cfg)

    def test_default_is_home_config_investigation(self):
        with mock.patch.dict(os.environ, {**self.env(),
                                          "INVESTIGATION_CONFIG_HOME": ""}):
            self.assertEqual(
                _config.config_home(),
                self.home / ".config" / "investigation")


class TestLoadMapping(IsolatedEnv):
    def test_missing_file_is_empty(self):
        with mock.patch.dict(os.environ, self.env()):
            self.assertEqual(_config._load_mapping(self.cfg / "none.yaml"), {})

    def test_empty_document_is_empty(self):
        p = self.cfg / "config.yaml"
        p.write_text("")  # safe_load -> None
        with mock.patch.dict(os.environ, self.env()):
            self.assertEqual(_config.load_machine_config(), {})

    def test_non_mapping_top_level_is_config_error(self):
        p = self.cfg / "config.yaml"
        p.write_text("- just\n- a list\n")
        with mock.patch.dict(os.environ, self.env()):
            with self.assertRaises(_config.ConfigError) as cm:
                _config.load_machine_config()
            self.assertIn("top level must be a mapping", str(cm.exception))
            self.assertIn(str(p), str(cm.exception))

    def test_mapping_document_loads(self):
        (self.cfg / "config.yaml").write_text("runs_dir: /tmp/x\n")
        with mock.patch.dict(os.environ, self.env()):
            self.assertEqual(_config.load_machine_config(),
                             {"runs_dir": "/tmp/x"})


class TestGitFace(IsolatedEnv):
    def test_oserror_face_returns_none(self):
        # git() must swallow a missing git executable (OSError on exec) and
        # return None, the same value it returns for a failing git command —
        # repo_toplevel/repo_name then degrade to their fallbacks.
        real_run = subprocess.run

        def fake_run(argv, **k):
            if argv and argv[0] == "git":
                raise OSError("no such file or directory: git")
            return real_run(argv, **k)

        with mock.patch.dict(os.environ, self.env()), \
                mock.patch.object(subprocess, "run", side_effect=fake_run):
            self.assertIsNone(_config.git("status"))

    def test_timeout_face_returns_none(self):
        with mock.patch.dict(os.environ, self.env()), \
                mock.patch.object(subprocess, "run",
                                  side_effect=subprocess.TimeoutExpired(
                                      "git", 30)):
            self.assertIsNone(_config.git("status"))

    def test_failing_command_returns_none_and_strips_success(self):
        with mock.patch.dict(os.environ, self.env()):
            self.assertIsNone(_config.git("rev-parse", "--verify",
                                          "no-such-ref-anywhere"))
            self.assertEqual(_config.git("rev-parse", "--show-toplevel"),
                             str(REPO))


class TestRunsRoot(IsolatedEnv):
    def test_env_var_wins(self):
        with mock.patch.dict(os.environ,
                             {**self.env(), "INVESTIGATION_RUNS": "/env/runs"}):
            self.assertEqual(_config.runs_root(), Path("/env/runs"))

    def test_unparseable_machine_config_degrades_to_default_runs(self):
        # runs_root(None) with a broken machine config must not crash: the
        # caller asked where runs live, not whether the config parses.
        (self.cfg / "config.yaml").write_text("foo: [unclosed\n")
        with mock.patch.dict(os.environ, self.env()):
            self.assertEqual(_config.runs_root(),
                             Path("~/.cache/investigation").expanduser())

    def test_machine_runs_dir_is_used(self):
        (self.cfg / "config.yaml").write_text(f"runs_dir: {self.tmp}/mrd\n")
        with mock.patch.dict(os.environ, self.env()):
            self.assertEqual(_config.runs_root(), self.tmp / "mrd")

    def test_empty_machine_runs_dir_falls_back_to_default(self):
        # runs_dir: null -> safe_load -> {"runs_dir": None} -> DEFAULT_RUNS,
        # the same fallback as a machine config with no runs_dir key at all.
        (self.cfg / "config.yaml").write_text("runs_dir:\n")
        with mock.patch.dict(os.environ, self.env()):
            self.assertEqual(_config.runs_root(),
                             Path("~/.cache/investigation").expanduser())

    def test_passed_machine_dict_short_circuits_load(self):
        # An explicit machine dict must be used as-is: no config read.
        with mock.patch.dict(os.environ, self.env()), \
                mock.patch.object(_config, "load_machine_config",
                                  side_effect=AssertionError(
                                      "must not be called")):
            self.assertEqual(_config.runs_root({"runs_dir": "/explicit"}),
                             Path("/explicit"))


if __name__ == "__main__":
    unittest.main()
