"""Tests for skills/investigation/scripts/doctor.py — preflight.

Every test runs doctor in a subprocess with a fake $HOME, a fake
$INVESTIGATION_CONFIG_HOME and a PATH holding only `git`, against fake git
repos in a tempdir. --offline throughout: no test touches the network.
"""
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills/investigation/scripts"
DOCTOR = SCRIPTS / "doctor.py"

sys.path.insert(0, str(SCRIPTS))
import doctor  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cov_child_env import covsite_env  # noqa: E402

METHODS = {"code-reading", "telemetry-snapshot", "spotlights-static",
           "spotlights-telemetry", "nous-blis", "sim2real-prep"}

try:
    import yaml  # noqa: F401
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


def sh(*cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def make_repo(path: Path, remotes: dict | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    sh("git", "init", "-q", "-b", "main", cwd=path)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
       "--allow-empty", "-m", "init", cwd=path)
    for name, url in (remotes or {}).items():
        sh("git", "remote", "add", name, url, cwd=path)
    return path


class DoctorEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.cfg = self.tmp / "cfg"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        os.symlink(shutil.which("git"), self.bin / "git")
        # The runtime needs process start times: `ps` off Linux (procid.py).
        if shutil.which("ps"):
            os.symlink(shutil.which("ps"), self.bin / "ps")
        self.env = {"HOME": str(self.home), "PATH": str(self.bin),
                    "INVESTIGATION_CONFIG_HOME": str(self.cfg),
                    "GIT_CONFIG_NOSYSTEM": "1", "LANG": "C.UTF-8",
                    **{k: v for k, v in os.environ.items()
                     if k.startswith("COVERAGE_")},
                    **covsite_env()}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def doctor(self, cwd, *args, env_extra=None):
        env = dict(self.env, **(env_extra or {}))
        return subprocess.run([sys.executable, str(DOCTOR), "--offline", *args],
                              cwd=cwd, env=env, capture_output=True, text=True)

    def doctor_json(self, cwd, env_extra=None):
        r = self.doctor(cwd, "--json", env_extra=env_extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def write_machine(self, text):
        self.cfg.mkdir(parents=True, exist_ok=True)
        (self.cfg / "config.yaml").write_text(textwrap.dedent(text))


class TestJsonShape(DoctorEnv):
    def test_shape_and_methods_map(self):
        repo = make_repo(self.tmp / "target", {"origin": "git@example.com:org/target.git"})
        r = self.doctor_json(repo)
        for key in ("python", "tools", "repo", "config", "pins", "engines", "blis",
                    "runs", "methods", "warnings", "unconfigured"):
            self.assertIn(key, r)
        self.assertEqual(set(r["methods"]), METHODS)
        for name, m in r["methods"].items():
            self.assertIsInstance(m["available"], bool, name)
            if not m["available"]:
                self.assertTrue(m.get("reason"), f"{name} unavailable without a reason")
        self.assertEqual(set(r["engines"]),
                         {"nous", "spotlights-engine", "signal-pipeline", "sim2real"})
        self.assertTrue(r["methods"]["code-reading"]["available"])
        self.assertEqual(Path(r["repo"]["toplevel"]).resolve(), repo.resolve())
        self.assertEqual(r["repo"]["name"], "target")
        self.assertEqual(r["pins"].get("nous-pin"), "nous-v2-fixes@e224cc1")
        self.assertEqual(r["pins"].get("spotlights-pin"), "v0.1.1@cccc290")
        self.assertFalse(r["config"]["machine"]["present"])
        self.assertTrue(r["offline"])

    def test_human_output_lists_methods(self):
        repo = make_repo(self.tmp / "target")
        r = self.doctor(repo)
        self.assertEqual(r.returncode, 0, r.stderr)
        for m in METHODS:
            self.assertIn(m, r.stdout)
        self.assertIn("Unconfigured", r.stdout)

    def test_not_a_git_repo_exits_nonzero(self):
        d = self.tmp / "plain"
        d.mkdir()
        r = self.doctor(d, "--json", env_extra={"GIT_CEILING_DIRECTORIES": str(self.tmp)})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("error", json.loads(r.stdout))


class TestMissingEngines(DoctorEnv):
    def test_nothing_installed(self):
        repo = make_repo(self.tmp / "target")
        r = self.doctor_json(repo)
        for name, e in r["engines"].items():
            self.assertFalse(e["found"], name)
        m = r["methods"]
        self.assertFalse(m["spotlights-static"]["available"])
        self.assertIn("spotlights-engine not found", m["spotlights-static"]["reason"])
        self.assertFalse(m["nous-blis"]["available"])
        self.assertIn("nous not found", m["nous-blis"]["reason"])
        self.assertIn("no BLIS apparatus configured", m["nous-blis"]["reason"])
        self.assertFalse(m["sim2real-prep"]["available"])
        self.assertFalse(m["telemetry-snapshot"]["available"])  # no kubectl, no url cluster
        self.assertIn("engine nous", r["unconfigured"])

    def test_configured_path_missing(self):
        repo = make_repo(self.tmp / "target")
        if not HAVE_YAML:
            self.skipTest("PyYAML not installed")
        self.write_machine(f"engines:\n  spotlights-engine: {self.tmp / 'nope'}\n")
        r = self.doctor_json(repo)
        e = r["engines"]["spotlights-engine"]
        self.assertFalse(e["found"])
        self.assertTrue(any("does not exist" in w for w in e["warnings"]))


@unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
class TestConfiguredEngines(DoctorEnv):
    def test_checkout_pin_mismatch_and_unpushed_warns(self):
        """F7: a spotlights checkout on an unpushed branch, not at the pin."""
        sp = make_repo(self.tmp / "spotlights")
        (sp / "src/spotlights_engine/signal_pipeline").mkdir(parents=True)
        sh("git", "checkout", "-q", "-b", "skill/spotlights-run", cwd=sp)
        apparatus = self.tmp / "blis"
        apparatus.mkdir()
        self.write_machine(f"""\
            engines:
              spotlights-engine: {sp}
            blis_apparatus_root: {apparatus}
            runs_dir: {self.tmp / 'runs'}
            """)
        repo = make_repo(self.tmp / "target")
        r = self.doctor_json(repo)
        e = r["engines"]["spotlights-engine"]
        self.assertTrue(e["found"])
        self.assertEqual(e["source"], "config")
        self.assertEqual(e["ref"], "skill/spotlights-run")
        self.assertTrue(any("differs from the pin v0.1.1@cccc290" in w for w in e["warnings"]))
        self.assertTrue(any("unpushed" in w for w in e["warnings"]))
        # signal-pipeline defaults to the spotlights checkout
        self.assertTrue(r["engines"]["signal-pipeline"]["found"])
        self.assertTrue(r["methods"]["spotlights-static"]["available"])
        st = r["methods"]["spotlights-telemetry"]
        self.assertFalse(st["available"])
        self.assertIn("metrics-only", st["reason"])
        self.assertIn("traces.jsonl", st["reason"])
        self.assertIn("01_signals.json", st["reason"])
        self.assertTrue(r["blis"]["exists"])
        self.assertEqual(r["runs"]["source"], "machine config runs_dir")
        self.assertTrue(any("spotlights-engine:" in w for w in r["warnings"]))
        # nous still missing -> nous-blis unavailable, but not for the apparatus
        self.assertNotIn("BLIS apparatus", r["methods"]["nous-blis"]["reason"])

    def test_unparseable_machine_config(self):
        self.write_machine("engines: [unclosed\n")
        repo = make_repo(self.tmp / "target")
        r = self.doctor_json(repo)
        self.assertTrue(r["config"]["machine"]["present"])
        self.assertFalse(r["config"]["machine"]["parseable"])
        self.assertTrue(any("can't be parsed" in w for w in r["warnings"]))

    def test_repo_config_keys_and_blis_applicable(self):
        repo = make_repo(self.tmp / "target")
        (repo / "investigations").mkdir()
        (repo / "investigations/config.yaml").write_text(
            "tracking: fork\nblis_applicable: false\nupstream_remote: origin\n")
        r = self.doctor_json(repo)
        self.assertEqual(r["config"]["repo"]["location"], "committed")
        self.assertEqual(r["config"]["repo"]["keys"],
                         ["blis_applicable", "tracking", "upstream_remote"])
        self.assertIn("blis_applicable: false", r["methods"]["nous-blis"]["reason"])


class TestUpstreamHeuristic(DoctorEnv):
    def test_named_upstream(self):
        repo = make_repo(self.tmp / "t", {"origin": "git@x:me/t.git", "upstream": "git@x:org/t.git"})
        r = self.doctor_json(repo)["repo"]
        self.assertEqual(r["upstream_remote"], "upstream")
        self.assertEqual(r["fork_remotes"], ["origin"])
        self.assertEqual(r["push_remote"], "origin")
        self.assertFalse(r["push_is_upstream"])

    def test_origin_only_is_upstream(self):
        repo = make_repo(self.tmp / "t", {"origin": "git@x:org/t.git"})
        r = self.doctor_json(repo)["repo"]
        self.assertEqual(r["upstream_remote"], "origin")
        self.assertTrue(r["push_is_upstream"])

    def test_ambiguous(self):
        repo = make_repo(self.tmp / "t", {"origin": "git@x:me/t.git", "kelly": "git@x:k/t.git"})
        r = self.doctor_json(repo)
        self.assertIsNone(r["repo"]["upstream_remote"])
        self.assertTrue(any("upstream remote unknown" in w for w in r["warnings"]))

    @unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
    def test_config_upstream_remote_wins(self):
        repo = make_repo(self.tmp / "t", {"origin": "git@x:me/t.git", "kelly": "git@x:k/t.git"})
        (repo / "investigations").mkdir()
        (repo / "investigations/config.yaml").write_text("tracking: fork\nupstream_remote: kelly\n")
        r = self.doctor_json(repo)["repo"]
        self.assertEqual(r["upstream_remote"], "kelly")
        self.assertFalse(r["push_is_upstream"])


class TestWithoutPyYAML(DoctorEnv):
    def test_imports_and_reports_missing(self):
        repo = make_repo(self.tmp / "target")
        self.write_machine("engines: {}\n")
        code = textwrap.dedent(f"""\
            import sys
            class Block:
                def find_spec(self, name, path=None, target=None):
                    if name == "yaml" or name.startswith("yaml."):
                        raise ImportError("blocked")
            sys.meta_path.insert(0, Block())
            sys.path.insert(0, {str(SCRIPTS)!r})
            import doctor
            sys.exit(doctor.main(["--json", "--offline"]))
            """)
        r = subprocess.run([sys.executable, "-c", code], cwd=repo, env=self.env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertFalse(out["tools"]["pyyaml"]["available"])
        self.assertTrue(any("PyYAML missing" in w for w in out["warnings"]))
        self.assertFalse(out["config"]["machine"]["parseable"])
        self.assertTrue(out["methods"]["code-reading"]["available"])


RUNTIME_INIT = SCRIPTS / "ains_runtime/__init__.py"
GOVERNED = ("nous-blis", "spotlights-static")


def runtime_version_from_text() -> str:
    m = re.search(r'^RUNTIME_VERSION\s*=\s*"([^"]+)"', RUNTIME_INIT.read_text(), re.M)
    return m.group(1)


class TestRuntime(DoctorEnv):
    def test_runtime_block_shape(self):
        repo = make_repo(self.tmp / "target")
        r = self.doctor_json(repo)
        self.assertEqual(set(r["methods"]), METHODS)  # no "runtime" method
        rt = r["runtime"]
        for key in ("present", "version", "store_schema_version", "message_schemas",
                    "export_schema", "pin", "start_time_method", "available"):
            self.assertIn(key, rt)
        self.assertTrue(rt["present"])
        self.assertEqual(rt["version"], runtime_version_from_text())
        pin = doctor.read_pins().get("runtime-version")
        if pin:
            self.assertEqual(rt["pin"], pin)
            self.assertEqual(rt["version"], pin)
        self.assertIsInstance(rt["store_schema_version"], int)
        self.assertGreaterEqual(rt["store_schema_version"], 1)
        self.assertEqual(rt["message_schemas"].get("experiment-request"), [1])
        self.assertTrue(rt["export_schema"].startswith("ains.runtime-export/"))
        if shutil.which("ps") or sys.platform.startswith("linux"):
            self.assertIn(rt["start_time_method"], ("proc-stat", "ps-lstart"))
        if not rt["available"]:
            self.assertTrue(rt["reason"])
        self.assertIn("Runtime", self.doctor(repo).stdout)

    @unittest.skipIf(sys.platform.startswith("linux"), "/proc gives a method on Linux")
    def test_no_ps_blocks_governed_methods(self):
        (self.bin / "ps").unlink(missing_ok=True)
        repo = make_repo(self.tmp / "target")
        r = self.doctor_json(repo)
        rt = r["runtime"]
        self.assertIsNone(rt["start_time_method"])
        self.assertFalse(rt["available"])
        self.assertIn("start times", rt["reason"])
        for name in GOVERNED:
            self.assertFalse(r["methods"][name]["available"])
            self.assertIn("runtime unavailable", r["methods"][name]["reason"])
        # an already-unavailable method keeps its own reason too
        self.assertIn("nous not found", r["methods"]["nous-blis"]["reason"])
        # not governed yet (D10)
        self.assertNotIn("runtime", r["methods"]["spotlights-telemetry"]["reason"])


class TestRuntimeHelpers(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_runtime(self, version='"9.8.7"'):
        pkg = self.tmp / "ains_runtime"
        pkg.mkdir(exist_ok=True)
        (pkg / "__init__.py").write_text(textwrap.dedent(f"""\
            raise SystemExit("must not be executed")
            RUNTIME_VERSION = {version}
            SCHEMA_VERSIONS = {{"receipt": (1, 2)}}
            STORE_SCHEMA_VERSION: int = 3
            EXPORT_SCHEMA = "ains.runtime-export/v9"
            """))
        cli = self.tmp / "runtime.py"
        cli.write_text("")
        return pkg, cli

    def test_text_parser_reads_other_version(self):
        pkg, _ = self.fake_runtime()
        c = doctor.runtime_constants(pkg / "__init__.py")
        self.assertEqual(c, {"RUNTIME_VERSION": "9.8.7", "SCHEMA_VERSIONS": {"receipt": (1, 2)},
                             "STORE_SCHEMA_VERSION": 3,
                             "EXPORT_SCHEMA": "ains.runtime-export/v9"})

    def test_runtime_info_pin_mismatch_warns(self):
        pkg, cli = self.fake_runtime()
        rt = doctor.runtime_info({"runtime-version": "0.1.0"}, True, pkg, cli, "ps-lstart")
        self.assertTrue(rt["available"])
        self.assertEqual(rt["message_schemas"], {"receipt": [1, 2]})
        self.assertEqual(rt["store_schema_version"], 3)
        self.assertTrue(any("differs from the SKILL.md pin" in w for w in rt["warnings"]))

    def test_runtime_info_unavailable_reasons(self):
        rt = doctor.runtime_info({}, True, self.tmp / "nope", self.tmp / "nope.py", "ps-lstart")
        self.assertFalse(rt["present"])
        self.assertIn("runtime files missing", rt["reason"])
        pkg, cli = self.fake_runtime(version="1")
        rt = doctor.runtime_info({}, True, pkg, cli, "ps-lstart")
        self.assertIn("RUNTIME_VERSION", rt["reason"])
        pkg, cli = self.fake_runtime()
        rt = doctor.runtime_info({}, False, pkg, cli, None)
        self.assertFalse(rt["available"])
        self.assertIn("start times", rt["reason"])
        self.assertIn("PyYAML", rt["reason"])

    def test_start_time_method(self):
        stat = self.tmp / "stat"
        stat.write_text("x")
        none = lambda _: None  # noqa: E731
        ps = lambda _: "/bin/ps"  # noqa: E731
        self.assertEqual(doctor.start_time_method("linux", stat, none), "proc-stat")
        self.assertEqual(doctor.start_time_method("linux", self.tmp / "no", ps), "ps-lstart")
        self.assertEqual(doctor.start_time_method("darwin", stat, ps), "ps-lstart")
        self.assertIsNone(doctor.start_time_method("darwin", stat, none))
        self.assertIsNone(doctor.start_time_method("linux", self.tmp / "no", none))

    def test_gate_on_runtime(self):
        m = {"nous-blis": {"available": False, "reason": "nous not found"},
             "spotlights-static": {"available": True},
             "spotlights-telemetry": {"available": False, "reason": "x"},
             "sim2real-prep": {"available": True}}
        doctor.gate_on_runtime(m, {"available": False, "reason": "no ps"})
        self.assertEqual(m["nous-blis"]["reason"], "nous not found; runtime unavailable: no ps")
        self.assertEqual(m["spotlights-static"],
                         {"available": False, "reason": "runtime unavailable: no ps"})
        self.assertEqual(m["spotlights-telemetry"]["reason"], "x")
        self.assertFalse(m["sim2real-prep"]["available"])

    def test_doctor_never_imports_runtime(self):
        tree = ast.parse(DOCTOR.read_text())
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names.append(node.module or "")
        self.assertTrue(names)
        self.assertFalse([n for n in names if "ains_runtime" in n], names)
        self.assertNotIn("importlib", names)


class TestHelpers(unittest.TestCase):
    def test_read_pins_from_skill_md(self):
        pins = doctor.read_pins()
        self.assertEqual(pins["nous-pin"], "nous-v2-fixes@e224cc1")
        self.assertEqual(pins["spotlights-pin"], "v0.1.1@cccc290")
        self.assertEqual(pins["sim2real-pin"], "main@28a8e98")
        self.assertEqual(pins["version"], "2.0.0")

    def test_same_sha(self):
        self.assertTrue(doctor.same_sha("cccc290f8dec", "cccc290"))
        self.assertFalse(doctor.same_sha("e7aff25", "cccc290"))
        self.assertFalse(doctor.same_sha(None, "cccc290"))

    def test_version_key_orders_tags(self):
        self.assertEqual(max(["v0.1.0", "v0.1.10", "v0.1.9"], key=doctor.version_key), "v0.1.10")


if __name__ == "__main__":
    unittest.main()
