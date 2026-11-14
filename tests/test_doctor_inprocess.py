"""In-process tests for doctor.py's report assembly: collect(), methods(),
human() and main().

test_doctor.py drives the script as an env-scrubbed subprocess (so it sees the
report only through stdout) and test_doctor_parsers.py drives the per-engine
helpers directly. Between them, collect()'s assembly, methods()' availability
rules, human()'s rendering and main()'s exit paths are only ever executed in
child processes whose coverage is lost to the scrubbed environment — doctor.py
read 56% at main 0cb1552 while every helper it calls was individually covered.

These tests import doctor in-process and drive the same scenarios with
env-vars swapped per test (the mock.patch.dict pattern test_runtime_nous.py
uses), so the report shape and every fallback string is asserted directly:

  * collect()  — config layers (machine/repo, home fallback, shadowed,
                unparseable), repo_info's remote heuristics, runs roots,
                warnings and unconfigured lists, engine wiring via checkouts
  * methods()  — every availability reason, the telemetry note, sim2real's
                nous-blis dependency
  * gate_on_runtime() — governed methods and the sim2real rewrite
  * human()    — every section and fallback string, including the ones the
                subprocess tests can't reach offline (latest-release lines)
  * main()     — both output modes and the not-a-repo exit path

No network: collect() runs with offline=True throughout.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills/investigation/scripts"

sys.path.insert(0, str(SCRIPTS))
import doctor  # noqa: E402


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


def fake_report(**overrides) -> dict:
    """A minimal but well-formed collect() result for human()-render tests."""
    r = {"doctor_version": 1, "offline": True, "skill_dir": "/skill",
         "python": {"version": "3.12.0", "ok": True, "executable": "/bin/python"},
         "tools": {"pyyaml": {"available": True, "version": "6.0.1"},
                   "git": {"path": "/bin/git", "version": "git version 2.47"},
                   "kubectl": {"path": "/bin/kubectl", "version": "v1.30"},
                   "npx": {"path": "/bin/npx", "version": "10.9"}},
         "repo": {"name": "t", "toplevel": "/t", "branch": "main", "head": "a" * 40,
                  "remotes": {"origin": "git@x:org/t.git"}, "upstream_remote": "origin",
                  "upstream_reason": "origin is the only remote", "fork_remotes": [],
                  "push_remote": "origin", "push_is_upstream": True},
         "config": {"machine": {"path": "/m", "location": "machine", "present": True,
                                "parseable": True, "keys": ["runs_dir"], "error": None,
                                "tracking": None},
                    "repo": {"path": "/r", "location": "committed", "present": True,
                             "parseable": True, "keys": ["tracking"], "error": None,
                             "tracking": "fork"}},
         "pins": {"nous-pin": "main@aaaaaaa"},
         "engines": {"nous": {"found": True, "source": "config", "path": "/eng",
                              "version": "v1", "sha": "a" * 40, "ref": "main",
                              "pin": "main@aaaaaaa",
                              "latest_release": {"tag": "v9.9.9", "sha": "b" * 40},
                              "latest_checked": True, "warnings": []}},
         "blis": {"apparatus_root": "/blis", "configured": True, "exists": True},
         "runs": {"root": "/runs", "study_runs": "/runs/t/<slug>", "source": "default",
                  "free_bytes": 5_000_000_000, "free_gb": 5.0},
         "runtime": {"present": True, "package": "/p", "cli": "/c", "version": "0.1.0",
                     "store_schema_version": 1, "message_schemas": {"m": [1]},
                     "export_schema": "ains.runtime-export/v1", "pin": "0.1.0",
                     "start_time_method": "proc-stat", "available": True, "warnings": []},
         "methods": {"code-reading": {"available": True}},
         "warnings": ["w-1"], "unconfigured": ["u-1"]}
    r.update(overrides)
    return r


class InProcessEnv(unittest.TestCase):
    """Base: a temp $HOME/$INVESTIGATION_CONFIG_HOME/$INVESTIGATION_RUNS per test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.cfg = self.tmp / "cfg"
        self.cfg.mkdir()
        self.runs = self.tmp / "runs"

    def env(self, **extra):
        base = {"HOME": str(self.home), "INVESTIGATION_CONFIG_HOME": str(self.cfg),
                "INVESTIGATION_RUNS": str(self.runs),
                "GIT_CONFIG_NOSYSTEM": "1"}
        base.update(extra)
        return base

    def collect(self, repo: Path, offline=True, **extra_env):
        with mock.patch.dict(os.environ, self.env(**extra_env)):
            return doctor.collect(cwd=repo, offline=offline)


class TestCollectConfigLayers(InProcessEnv):
    def test_no_configs_reports_both_layers_missing(self):
        repo = make_repo(self.tmp / "t")
        r = self.collect(repo)
        self.assertFalse(r["config"]["machine"]["present"])
        self.assertEqual(r["config"]["repo"]["location"], "none")
        self.assertEqual(r["config"]["repo"]["searched"],
                         [str(repo / "investigations/config.yaml"),
                          str(self.cfg / "repos/t.yaml")])
        self.assertTrue(any("no machine config" in u for u in r["unconfigured"]))
        self.assertTrue(any("new_study.py writes it" in u for u in r["unconfigured"]))

    def test_unparseable_machine_config_warns(self):
        (self.cfg / "config.yaml").write_text("engines: [unclosed\n")
        repo = make_repo(self.tmp / "t")
        r = self.collect(repo)
        self.assertFalse(r["config"]["machine"]["parseable"])
        self.assertIn("error", r["config"]["machine"])
        self.assertTrue(any("can't be parsed" in w for w in r["warnings"]))

    def test_machine_config_keys_listed(self):
        (self.cfg / "config.yaml").write_text(
            "runs_dir: /r\nrepos:\n  a: /a\nmodels:\n  nous_session: x\n")
        repo = make_repo(self.tmp / "t")
        r = self.collect(repo)
        self.assertEqual(r["config"]["machine"]["keys"], ["models", "repos", "runs_dir"])

    def test_committed_repo_config_wins_and_shadows_home(self):
        repo = make_repo(self.tmp / "t")
        (repo / "investigations").mkdir()
        (repo / "investigations/config.yaml").write_text("tracking: fork\n")
        home_cfg = self.cfg / "repos/t.yaml"
        home_cfg.parent.mkdir(parents=True)
        home_cfg.write_text("tracking: untracked\n")
        r = self.collect(repo)
        self.assertEqual(r["config"]["repo"]["location"], "committed")
        self.assertEqual(r["config"]["repo"]["tracking"], "fork")
        self.assertEqual(r["config"]["repo"]["shadowed"], str(home_cfg))

    def test_home_repo_config_used_when_no_committed_one(self):
        repo = make_repo(self.tmp / "t")
        home_cfg = self.cfg / "repos/t.yaml"
        home_cfg.parent.mkdir(parents=True)
        home_cfg.write_text("tracking: untracked\n")
        r = self.collect(repo)
        self.assertEqual(r["config"]["repo"]["location"], "home")
        self.assertEqual(r["config"]["repo"]["keys"], ["tracking"])

    def test_unparseable_repo_config_warns(self):
        repo = make_repo(self.tmp / "t")
        (repo / "investigations").mkdir()
        (repo / "investigations/config.yaml").write_text("bad: [y\n")
        r = self.collect(repo)
        self.assertFalse(r["config"]["repo"]["parseable"])
        self.assertTrue(any("can't be parsed" in w for w in r["warnings"]))


class TestCollectRepo(InProcessEnv):
    def test_no_remotes_reports_none_and_warns(self):
        repo = make_repo(self.tmp / "t")
        r = self.collect(repo)
        self.assertEqual(r["repo"]["remotes"], {})
        self.assertEqual(r["repo"]["upstream_reason"], "no remotes")
        self.assertIsNone(r["repo"]["push_remote"])
        self.assertTrue(any("upstream remote unknown" in w for w in r["warnings"]))

    def test_detached_head_still_pushes_to_origin(self):
        repo = make_repo(self.tmp / "t", {"origin": "git@x:org/t.git"})
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                              capture_output=True, text=True).stdout.strip()
        sh("git", "checkout", "-q", "--detach", head, cwd=repo)
        r = self.collect(repo)
        self.assertIsNone(r["repo"]["branch"])
        self.assertEqual(r["repo"]["push_remote"], "origin")

    def test_configured_upstream_missing_from_remotes_warns(self):
        repo = make_repo(self.tmp / "t", {"origin": "git@x:me/t.git",
                                          "kelly": "git@x:k/t.git"})
        ri = doctor.repo_info(repo, {"upstream_remote": "ghost"})
        self.assertEqual(ri["upstream_remote"], "ghost")
        self.assertIn("no remote named 'ghost'", ri["upstream_reason"])

    def test_push_remote_ladder_reads_git_config(self):
        repo = make_repo(self.tmp / "t", {"origin": "git@x:org/t.git",
                                          "kelly": "git@x:k/t.git"})
        sh("git", "config", "branch.main.pushRemote", "kelly", cwd=repo)
        self.assertEqual(doctor.repo_info(repo, {})["push_remote"], "kelly")
        sh("git", "config", "--unset", "branch.main.pushRemote", cwd=repo)
        sh("git", "config", "remote.pushDefault", "kelly", cwd=repo)
        self.assertEqual(doctor.repo_info(repo, {})["push_remote"], "kelly")
        sh("git", "config", "--unset", "remote.pushDefault", cwd=repo)
        self.assertEqual(doctor.repo_info(repo, {})["push_remote"], "origin")

    def test_push_is_upstream_when_push_remote_is_the_upstream(self):
        repo = make_repo(self.tmp / "t", {"origin": "git@x:org/t.git"})
        sh("git", "config", "branch.main.pushRemote", "origin", cwd=repo)
        r = self.collect(repo)
        self.assertTrue(r["repo"]["push_is_upstream"])
        self.assertIn("UPSTREAM: commit guard will refuse",
                      doctor.human(r))


class TestCollectRuns(InProcessEnv):
    def test_runs_root_sources_in_order(self):
        repo = make_repo(self.tmp / "t")
        self.assertEqual(self.collect(repo)["runs"]["source"], "INVESTIGATION_RUNS")
        env = self.env()
        env.pop("INVESTIGATION_RUNS")
        (self.cfg / "config.yaml").write_text(f"runs_dir: {self.tmp / 'rd'}\n")
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("INVESTIGATION_RUNS", None)
            r = doctor.collect(cwd=repo, offline=True)
        self.assertEqual(r["runs"]["source"], "machine config runs_dir")

    def test_missing_runs_root_measures_the_nearest_existing_ancestor(self):
        repo = make_repo(self.tmp / "t")
        (self.cfg / "config.yaml").write_text(
            f"runs_dir: {self.tmp / 'a' / 'b' / 'c'}\n")
        env = self.env()
        env.pop("INVESTIGATION_RUNS")
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("INVESTIGATION_RUNS", None)
            r = doctor.collect(cwd=repo, offline=True)
        self.assertEqual(Path(r["runs"]["measured_at"]), self.tmp)
        self.assertIsNotNone(r["runs"]["free_gb"])

    def test_free_space_unmeasurable_still_reports(self):
        with mock.patch.object(doctor.shutil, "disk_usage", side_effect=OSError):
            fs = doctor.free_space(Path("/no/such/xyz/nested"))
        self.assertIsNone(fs["free_bytes"])
        self.assertIsNone(fs["free_gb"])


class TestCollectEngines(InProcessEnv):
    def test_configured_checkouts_drive_the_report(self):
        eng = make_repo(self.tmp / "eng", {"origin": "git@x:org/eng.git"})
        (eng / "src/spotlights_engine/signal_pipeline").mkdir(parents=True)
        (self.cfg / "config.yaml").write_text(textwrap.dedent(f"""\
            engines:
              nous: {eng}
              spotlights-engine: {eng}
              signal-pipeline: {eng}
              sim2real: {eng}
            blis_apparatus_root: {eng}
            runs_dir: {self.tmp / 'rd'}
            """))
        repo = make_repo(self.tmp / "t", {"origin": "git@x:me/t.git",
                                          "upstream": "git@x:org/t.git"})
        (repo / "investigations").mkdir()
        (repo / "investigations/config.yaml").write_text(
            "tracking: fork\nclusters:\n  prod:\n    kind: url\n")
        r = self.collect(repo)
        for name, e in r["engines"].items():
            self.assertTrue(e["found"], name)
            self.assertEqual(e["source"], "config")
        self.assertTrue(r["blis"]["exists"])
        self.assertEqual(r["unconfigured"], [])
        m = r["methods"]
        self.assertTrue(m["code-reading"]["available"])
        self.assertTrue(m["telemetry-snapshot"]["available"])
        self.assertTrue(m["spotlights-static"]["available"])
        self.assertTrue(m["nous-blis"]["available"])
        self.assertTrue(m["sim2real-prep"]["available"])
        self.assertFalse(m["spotlights-telemetry"]["available"])  # by design (metrics only)

    def test_engine_warnings_are_prefixed_in_the_report(self):
        eng = make_repo(self.tmp / "eng")
        (self.cfg / "config.yaml").write_text(f"engines:\n  sim2real: {eng}\n")
        repo = make_repo(self.tmp / "t")
        r = self.collect(repo)
        self.assertTrue(any(w.startswith("sim2real: ") and "unpushed" in w
                            for w in r["warnings"]))

    def test_unconfigured_engines_listed(self):
        repo = make_repo(self.tmp / "t")
        r = self.collect(repo)
        for name in ("engine nous", "engine spotlights-engine",
                     "engine signal-pipeline", "engine sim2real"):
            self.assertIn(name, r["unconfigured"])

    def test_engines_section_not_a_dict_is_ignored(self):
        (self.cfg / "config.yaml").write_text("engines: not-a-mapping\n")
        repo = make_repo(self.tmp / "t")
        r = self.collect(repo)
        self.assertFalse(r["engines"]["nous"]["found"])

    def test_blis_apparatus_root_expanduser_and_missing(self):
        (self.cfg / "config.yaml").write_text("blis_apparatus_root: ~/apparatus\n")
        repo = make_repo(self.tmp / "t")
        r = self.collect(repo)
        self.assertEqual(r["blis"]["apparatus_root"], str(self.home / "apparatus"))
        self.assertTrue(r["blis"]["configured"])
        self.assertFalse(r["blis"]["exists"])

    def test_blis_applicable_false_disables_nous_blis(self):
        repo = make_repo(self.tmp / "t")
        (repo / "investigations").mkdir()
        (repo / "investigations/config.yaml").write_text("blis_applicable: false\n")
        r = self.collect(repo)
        self.assertIn("blis_applicable: false", r["methods"]["nous-blis"]["reason"])

    def test_runtime_pin_mismatch_warns(self):
        repo = make_repo(self.tmp / "t")
        with mock.patch.object(doctor, "read_pins",
                               return_value={"runtime-version": "0.0.0-else"}):
            r = self.collect(repo)
        self.assertTrue(any(w.startswith("runtime: ") and "differs" in w
                            for w in r["warnings"]))


class TestCollectPythonAndPyyaml(InProcessEnv):
    def test_old_python_warns(self):
        repo = make_repo(self.tmp / "t")
        with mock.patch.object(doctor.sys, "version_info", (3, 9, 1, "final", 0)):
            r = self.collect(repo)
        self.assertFalse(r["python"]["ok"])
        self.assertEqual(r["warnings"][0], "Python 3.9.1 is older than 3.10")
        self.assertIn("NEEDS 3.10+", doctor.human(r))

    def test_missing_pyyaml_reports_everywhere(self):
        repo = make_repo(self.tmp / "t")
        import builtins
        real_import = builtins.__import__

        def block_yaml(name, *a, **k):
            if name == "yaml" or name.startswith("yaml."):
                raise ImportError("blocked")
            return real_import(name, *a, **k)

        builtins.__import__ = block_yaml
        try:
            r = self.collect(repo)
        finally:
            builtins.__import__ = real_import
        self.assertFalse(r["tools"]["pyyaml"]["available"])
        self.assertTrue(any("PyYAML missing" in w for w in r["warnings"]))
        self.assertIn("PyYAML missing", r["methods"]["telemetry-snapshot"]["reason"])
        self.assertIn("PyYAML missing", r["methods"]["nous-blis"]["reason"])
        self.assertIn("PyYAML", r["runtime"]["reason"])


class TestMethods(unittest.TestCase):
    """methods(): every availability rule, fed synthetic tool/engine/blis dicts."""

    TOOLS = {"pyyaml": {"available": True}, "git": {"path": "/bin/git"},
             "kubectl": {"path": None}, "npx": {"path": None}}
    ENGINES = {"nous": {"found": True}, "spotlights-engine": {"found": True},
               "signal-pipeline": {"found": True}, "sim2real": {"found": True}}
    BLIS = {"configured": True, "exists": True, "apparatus_root": "/blis"}

    def methods(self, tools=None, engines=None, blis=None, repo_cfg=None):
        return doctor.methods(tools or dict(self.TOOLS), engines or dict(self.ENGINES),
                              blis or dict(self.BLIS), repo_cfg or {}, {})

    def test_all_present_makes_every_launchable_method_available(self):
        m = self.methods()
        for name in ("code-reading", "telemetry-snapshot", "spotlights-static",
                     "nous-blis", "sim2real-prep"):
            self.assertIn(name, m)
        self.assertTrue(m["code-reading"]["available"])
        self.assertTrue(m["nous-blis"]["available"])
        self.assertTrue(m["sim2real-prep"]["available"])
        self.assertFalse(m["telemetry-snapshot"]["available"])  # no kubectl, no url
        self.assertFalse(m["spotlights-telemetry"]["available"])  # by design

    def test_spotlights_telemetry_reason_names_the_stage01_fact_filenames(self):
        # The reason is built from the stage01_inputs fact, so the filenames
        # it names are the ones spotlights.json holds (cited to the locked
        # commit) — not a prose restatement that can drift on a lock bump.
        m = self.methods()
        reason = m["spotlights-telemetry"]["reason"]
        import upstreams as U
        f = U.fact("spotlights", "stage01_inputs")
        self.assertIn(f["otel_discriminator"], reason)
        self.assertIn(f["signals_files"][0], reason)
        self.assertEqual(reason, doctor._spotlights_telemetry_reason())
        # The fact-sourced build must equal the literal fallback: when the
        # facts file is unreadable doctor still reports the same words.
        self.assertEqual(reason, doctor.SPOTLIGHTS_TELEMETRY_REASON)

    def test_spotlights_telemetry_reason_falls_back_when_facts_unreadable(self):
        # doctor must still report with a broken/unreadable upstreams folder
        # (same resilience as read_pins); the fallback is the literal.
        import builtins
        real_import = builtins.__import__

        def blocked(name, *a, **k):
            if name == "upstreams":
                raise ImportError("blocked")
            return real_import(name, *a, **k)

        builtins.__import__ = blocked
        try:
            reason = doctor._spotlights_telemetry_reason()
        finally:
            builtins.__import__ = real_import
        self.assertEqual(reason, doctor.SPOTLIGHTS_TELEMETRY_REASON)
        self.assertIn("traces.jsonl", reason)
        self.assertIn("01_signals.json", reason)

    def test_git_missing_disables_code_reading(self):
        tools = dict(self.TOOLS, git={"path": None})
        m = self.methods(tools=tools)
        self.assertEqual(m["code-reading"], {"available": False, "reason": "git not found"})

    def test_url_cluster_enables_telemetry_without_kubectl(self):
        m = self.methods(repo_cfg={"clusters": {"prod": {"kind": "url"}}})
        self.assertTrue(m["telemetry-snapshot"]["available"])

    def test_non_url_cluster_alone_does_not(self):
        m = self.methods(repo_cfg={"clusters": {"prod": {"kind": "context"}}})
        self.assertIn("no kubectl", m["telemetry-snapshot"]["reason"])

    def test_kubectl_without_named_clusters_notes_it(self):
        tools = dict(self.TOOLS, kubectl={"path": "/bin/kubectl"})
        m = self.methods(tools=tools)
        self.assertTrue(m["telemetry-snapshot"]["available"])
        self.assertIn("no clusters named", m["telemetry-snapshot"]["note"])

    def test_missing_pyyaml_disables_telemetry_and_nous(self):
        tools = dict(self.TOOLS, pyyaml={"available": False})
        m = self.methods(tools=tools)
        self.assertIn("PyYAML missing", m["telemetry-snapshot"]["reason"])
        self.assertIn("PyYAML missing", m["nous-blis"]["reason"])

    def test_spotlights_missing_reason(self):
        engines = dict(self.ENGINES, **{"spotlights-engine": {"found": False}})
        m = self.methods(engines=engines)
        self.assertIn("spotlights-engine not found", m["spotlights-static"]["reason"])

    def test_signal_pipeline_missing_reason(self):
        engines = dict(self.ENGINES, **{"signal-pipeline": {"found": False}})
        m = self.methods(engines=engines)
        self.assertIn("signal-pipeline not found", m["spotlights-telemetry"]["reason"])

    def test_nous_blis_reasons_stack(self):
        engines = dict(self.ENGINES, **{"nous": {"found": False}})
        blis = {"configured": False, "exists": False, "apparatus_root": None}
        m = self.methods(engines=engines, blis=blis)
        self.assertIn("nous not found", m["nous-blis"]["reason"])
        self.assertIn("no BLIS apparatus configured", m["nous-blis"]["reason"])

    def test_apparatus_missing_dir_reason(self):
        blis = {"configured": True, "exists": False, "apparatus_root": "/nope"}
        m = self.methods(blis=blis)
        self.assertIn("BLIS apparatus root /nope does not exist",
                      m["nous-blis"]["reason"])

    def test_sim2real_needs_nous_blis(self):
        engines = dict(self.ENGINES, **{"nous": {"found": False}})
        m = self.methods(engines=engines)
        self.assertIn("needs nous-blis", m["sim2real-prep"]["reason"])

    def test_sim2real_unconfigured_reason(self):
        engines = dict(self.ENGINES, **{"sim2real": {"found": False}})
        m = self.methods(engines=engines)
        self.assertIn("no sim2real checkout configured", m["sim2real-prep"]["reason"])


class TestGateOnRuntime(unittest.TestCase):
    def test_governed_methods_unavailable_with_own_reason_kept(self):
        m = {"nous-blis": {"available": False, "reason": "nous not found"},
             "spotlights-static": {"available": True},
             "spotlights-telemetry": {"available": False, "reason": "x"},
             "sim2real-prep": {"available": True}}
        doctor.gate_on_runtime(m, {"available": False, "reason": "no ps"})
        self.assertEqual(m["nous-blis"]["reason"], "nous not found; runtime unavailable: no ps")
        self.assertEqual(m["spotlights-static"], {"available": False,
                                                  "reason": "runtime unavailable: no ps"})
        self.assertEqual(m["spotlights-telemetry"]["reason"], "x")  # not governed (D10)
        self.assertFalse(m["sim2real-prep"]["available"])
        self.assertIn("needs nous-blis", m["sim2real-prep"]["reason"])

    def test_available_runtime_is_a_passthrough(self):
        m = {"nous-blis": {"available": True}, "spotlights-static": {"available": True},
             "spotlights-telemetry": {"available": False, "reason": "x"},
             "sim2real-prep": {"available": True}}
        doctor.gate_on_runtime(m, {"available": True})
        self.assertTrue(m["sim2real-prep"]["available"])


class TestHuman(unittest.TestCase):
    def test_renders_every_section_from_a_full_report(self):
        out = doctor.human(fake_report())
        for section in ("Tools", "Repo", "Config", "Engines", "BLIS apparatus",
                        "Runs dir", "Runtime", "Methods", "Unconfigured", "Warnings"):
            self.assertIn(section, out, section)
        self.assertIn("latest release v9.9.9", out)
        self.assertIn("pin main@aaaaaaa", out)
        self.assertIn("UPSTREAM: commit guard will refuse", out)
        self.assertIn("free 5.0 GB", out)
        self.assertIn("start-time method proc-stat", out)

    def test_missing_state_renders_fallbacks(self):
        r = fake_report(
            python={"version": "3.9.1", "ok": False, "executable": "/p"},
            tools={"pyyaml": {"available": False, "version": None},
                   "git": {"path": None, "version": None},
                   "kubectl": {"path": None, "version": None},
                   "npx": {"path": None, "version": None}},
            repo={"name": "t", "toplevel": "/t", "branch": None, "head": None,
                  "remotes": {}, "upstream_remote": None, "upstream_reason": "no remotes",
                  "fork_remotes": [], "push_remote": None, "push_is_upstream": None},
            config={"machine": {"path": "/m", "location": "machine", "present": True,
                                "parseable": False, "keys": [], "error": "boom",
                                "tracking": None},
                    "repo": {"path": "/r", "location": "home", "present": True,
                             "parseable": True, "keys": ["k"], "error": None,
                             "tracking": "untracked"}},
            engines={"a": {"found": True, "source": "PATH", "path": "/a",
                           "version": None, "sha": "1" * 40, "ref": None, "pin": None,
                           "latest_release": None, "latest_checked": False,
                           "warnings": ["w"]},
                     "b": {"found": True, "source": "PATH", "path": "/b",
                           "version": None, "sha": None, "ref": "dev", "pin": None,
                           "latest_release": None, "latest_checked": False,
                           "upstream_url": "u", "warnings": []},
                     "c": {"found": False, "source": None, "path": None, "version": None,
                           "sha": None, "ref": None, "pin": None,
                           "latest_release": None, "latest_checked": False,
                           "warnings": []}},
            blis={"apparatus_root": "/nope", "configured": True, "exists": False},
            runs={"root": "/runs", "study_runs": "/runs/t/<slug>", "source": "default",
                  "free_bytes": None, "free_gb": None},
            runtime={"present": True, "package": "/p", "cli": "/c", "version": None,
                     "store_schema_version": None, "message_schemas": None,
                     "export_schema": None, "pin": None, "start_time_method": None,
                     "available": False, "reason": "no start times", "warnings": []},
            methods={"code-reading": {"available": False, "reason": "git not found"},
                     "telemetry-snapshot": {"available": True, "note": "note-x"}},
            warnings=[], unconfigured=[])
        r["offline"] = False  # the "latest release: unknown" line needs an online report
        out = doctor.human(r)
        self.assertIn("NEEDS 3.10+", out)
        self.assertIn("pyyaml   MISSING (pip install pyyaml)", out)
        self.assertIn("git      not found", out)
        self.assertIn("branch (detached) @ ?", out)
        self.assertIn("remotes: none", out)
        self.assertIn("upstream: UNKNOWN (no remotes)", out)
        self.assertIn("push remote: ?", out)
        self.assertIn("UNPARSEABLE: boom", out)
        self.assertIn("repo (home)", out)
        self.assertIn("tracking: untracked", out)
        self.assertIn("a                  @1111111  via PATH: /a", out)
        self.assertIn("WARNING: w", out)
        self.assertIn("b                  (dev)  via PATH: /b", out)
        self.assertIn("latest release: unknown (ls-remote failed or no tags)", out)
        self.assertIn("c                  not found", out)
        self.assertIn("BLIS apparatus  /nope  (MISSING)", out)
        self.assertIn("free ? GB", out)
        self.assertIn("Runtime  ?  UNAVAILABLE", out)
        self.assertIn("store schema ?; export ?; start-time method NONE", out)
        self.assertIn("reason: no start times", out)
        self.assertIn("note: note-x", out)
        self.assertIn("reason: git not found", out)

    def test_offline_report_hides_the_unknown_latest_line(self):
        r = fake_report()
        out = doctor.human(r)
        self.assertNotIn("latest release: unknown", out)
        r["offline"] = True
        r["engines"]["nous"]["latest_release"] = None
        r["engines"]["nous"]["upstream_url"] = "u"
        out = doctor.human(r)
        self.assertNotIn("latest release: unknown", out)
        r["offline"] = False
        out = doctor.human(r)
        self.assertIn("latest release: unknown (ls-remote failed or no tags)", out)


class TestMain(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self._old_cwd = os.getcwd()
        self._old_env = {k: os.environ.get(k) for k in
                         ("HOME", "INVESTIGATION_CONFIG_HOME", "INVESTIGATION_RUNS",
                          "GIT_CEILING_DIRECTORIES")}
        os.environ["HOME"] = str(self.tmp / "home")
        os.environ["INVESTIGATION_CONFIG_HOME"] = str(self.tmp / "cfg")
        os.environ["INVESTIGATION_RUNS"] = str(self.tmp / "runs")
        os.makedirs(os.environ["HOME"])

    def tearDown(self):
        os.chdir(self._old_cwd)
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_human_and_json_modes_exit_zero(self):
        repo = make_repo(self.tmp / "t")
        os.chdir(repo)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(doctor.main([]), 0)
        self.assertIn("investigation doctor", buf.getvalue())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(doctor.main(["--json"]), 0)
        r = json.loads(buf.getvalue())
        self.assertEqual(r["doctor_version"], doctor.DOCTOR_VERSION)

    def test_not_a_git_repo_exits_two_in_both_modes(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        os.environ["GIT_CEILING_DIRECTORIES"] = str(self.tmp)
        os.chdir(plain)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(doctor.main(["--json"]), 2)
        self.assertIn("error", json.loads(buf.getvalue()))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(doctor.main([]), 2)
        self.assertIn("not inside a git repo", err.getvalue())


class TestHelpers(unittest.TestCase):
    """Small helpers reachable only in-process."""

    def test_run_returns_none_on_oserror_and_timeout(self):
        with mock.patch.object(doctor.subprocess, "run", side_effect=OSError("x")):
            self.assertIsNone(doctor.run(["no-such-cmd"]))
        import subprocess as sp
        with mock.patch.object(doctor.subprocess, "run",
                               side_effect=sp.TimeoutExpired(["x"], 1)):
            self.assertIsNone(doctor.run(["x"], timeout=0))

    def test_run_nonzero_exit_returns_none(self):
        self.assertIsNone(doctor.run(["false"]))
        self.assertEqual(doctor.run(["echo", "hi"]), "hi")
        self.assertEqual(doctor.run(["pwd"], cwd="/tmp"), "/tmp")

    def test_same_sha_requires_seven_shared_characters(self):
        self.assertTrue(doctor.same_sha("aaaaaaa1", "aaaaaaa"))
        self.assertFalse(doctor.same_sha("shorta", "shortabcdef"))
        self.assertFalse(doctor.same_sha(None, "x"))
        self.assertFalse(doctor.same_sha("", "x"))

    def test_frontmatter_meta_edges(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        p = tmp / "m.md"
        p.write_text("not frontmatter\n")
        self.assertEqual(doctor._frontmatter_meta(p), {})
        p.write_text("")
        self.assertEqual(doctor._frontmatter_meta(p), {})
        self.assertEqual(doctor._frontmatter_meta(tmp / "nope.md"), {})
        p.write_text("---\nname: x\nother:\n  a: 1\n---\n")
        self.assertEqual(doctor._frontmatter_meta(p), {})
        p.write_text("---\nmetadata:\n  version: 1\nother: 2\n  skipped: 3\n---\n")
        self.assertEqual(doctor._frontmatter_meta(p), {"version": "1"})
        p.write_text("---\nmetadata:\n  version: \"1.2.3\"\n")
        self.assertEqual(doctor._frontmatter_meta(p), {"version": "1.2.3"})

    def test_read_pins_lock_json_wins_over_skill_md_pins(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        p = tmp / "SKILL.md"
        p.write_text("---\nmetadata:\n  version: 9\n  nous-pin: stale@0000000\n---\n")
        pins = doctor.read_pins(p)
        self.assertEqual(pins["version"], "9")
        self.assertEqual(pins["nous-pin"], "nous-v2-fixes@e224cc1")  # lock.json wins
        self.assertEqual(pins["spotlights-pin"], "v0.1.1@cccc290")

    def test_read_pins_unreadable_lock_is_not_fatal(self):
        import upstreams
        with mock.patch.object(upstreams, "pin", side_effect=Exception("boom")):
            self.assertIsNone(doctor.read_pins().get("nous-pin"))

    def test_split_pin_shapes(self):
        self.assertEqual(doctor.split_pin(None), (None, None))
        self.assertEqual(doctor.split_pin(""), (None, None))
        self.assertEqual(doctor.split_pin("ref@"), ("ref", None))
        self.assertEqual(doctor.split_pin("a@b@c"), ("a", "b@c"))

    def test_layer_direct(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        p = tmp / "c.yaml"
        p.write_text("b: 2\na: 1\n")
        info, data = doctor.layer(p, "machine")
        self.assertEqual(info["parseable"], True)
        self.assertEqual(info["keys"], ["a", "b"])
        self.assertEqual(data, {"a": 1, "b": 2})
        p.write_text("- just\n- a list\n")
        info, _ = doctor.layer(p, "machine")
        self.assertFalse(info["parseable"])
        self.assertIn("top level must be a mapping", info["error"])
        info, data = doctor.layer(tmp / "nope.yaml", "machine")
        self.assertFalse(info["present"])
        self.assertIsNone(info["parseable"])
        self.assertEqual(data, {})

    def test_tool_version_first_line_only(self):
        with mock.patch.object(doctor.shutil, "which", return_value="/bin/git"):
            with mock.patch.object(doctor, "run", return_value="l1\nl2\n"):
                self.assertEqual(doctor.tool("git", ["--version"])["version"], "l1")
            with mock.patch.object(doctor, "run", return_value=None):
                self.assertIsNone(doctor.tool("git", ["--version"])["version"])
        with mock.patch.object(doctor.shutil, "which", return_value=None):
            self.assertEqual(doctor.tool("nope"), {"path": None, "version": None})


class TestRuntimeHelpers(unittest.TestCase):
    """runtime_info()/runtime_constants() error and type-guard branches."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.pkg = self.tmp / "ains_runtime"
        self.pkg.mkdir()
        self.cli = self.tmp / "runtime.py"
        self.cli.write_text("")

    def init(self, text):
        (self.pkg / "__init__.py").write_text(textwrap.dedent(text))

    def test_missing_files_reason(self):
        rt = doctor.runtime_info({}, True, self.tmp / "nope", self.tmp / "nope.py",
                                 "ps-lstart")
        self.assertFalse(rt["present"])
        self.assertIn("runtime files missing", rt["reason"])

    def test_syntax_error_reason(self):
        self.init("def broken(:\n")
        rt = doctor.runtime_info({}, True, self.pkg, self.cli, "ps-lstart")
        self.assertIn("can't parse", rt["reason"])
        self.assertFalse(rt["available"])

    def test_value_error_reason(self):
        self.init("RUNTIME_VERSION = some_name\n")
        rt = doctor.runtime_info({}, True, self.pkg, self.cli, "ps-lstart")
        self.assertIn("can't parse", rt["reason"])

    def test_non_string_version_reason(self):
        self.init("RUNTIME_VERSION = 5\n")
        rt = doctor.runtime_info({}, True, self.pkg, self.cli, "ps-lstart")
        self.assertIn("no RUNTIME_VERSION string", rt["reason"])

    def test_type_guards_keep_mismatched_constants_out(self):
        self.init('RUNTIME_VERSION = "1"\nSCHEMA_VERSIONS = [1]\nSTORE_SCHEMA_VERSION = "x"\n')
        rt = doctor.runtime_info({}, True, self.pkg, self.cli, "ps-lstart")
        self.assertEqual(rt["version"], "1")
        self.assertIsNone(rt["message_schemas"])
        self.assertIsNone(rt["store_schema_version"])

    def test_pin_mismatch_warns(self):
        self.init('RUNTIME_VERSION = "9.8.7"\n')
        rt = doctor.runtime_info({"runtime-version": "0.1.0"}, True, self.pkg, self.cli,
                                 "ps-lstart")
        self.assertTrue(rt["available"])
        self.assertTrue(any("differs from the SKILL.md pin" in w for w in rt["warnings"]))

    def test_missing_start_times_and_pyyaml_reasons(self):
        self.init('RUNTIME_VERSION = "1"\n')
        rt = doctor.runtime_info({}, False, self.pkg, self.cli, None)
        self.assertFalse(rt["available"])
        self.assertIn("start times", rt["reason"])
        self.assertIn("PyYAML", rt["reason"])

    def test_constants_only_read_declared_names(self):
        self.init("""\
            A, B = 1, 2
            obj.attr = 3
            _ann: int
            STORE_SCHEMA_VERSION: int = 3
            EXPORT_SCHEMA = "e/v1"
            """)
        c = doctor.runtime_constants(self.pkg / "__init__.py")
        self.assertEqual(c, {"STORE_SCHEMA_VERSION": 3, "EXPORT_SCHEMA": "e/v1"})


class TestEngineInfoInProcess(unittest.TestCase):
    """engine_info branches the CLI tests only see in aggregate."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def engine(self, name, machine, pins=None, offline=True, cache=None):
        return doctor.engine_info(name, machine, pins or {}, offline,
                                  cache if cache is not None else {})

    def test_signal_pipeline_config_directly_with_package(self):
        eng = make_repo(self.tmp / "eng")
        (eng / "src/spotlights_engine/signal_pipeline").mkdir(parents=True)
        e = self.engine("signal-pipeline", {"engines": {"signal-pipeline": str(eng)}})
        self.assertTrue(e["found"])
        self.assertFalse(any("signal_pipeline" in w for w in e["warnings"]))

    def test_signal_pipeline_inherits_spotlights_pin(self):
        e = self.engine("signal-pipeline", {},
                        {"spotlights-pin": "v0.1.1@cccc290"})
        self.assertEqual(e["pin"], "v0.1.1@cccc290")

    def test_sim2real_has_no_pin_key(self):
        e = self.engine("sim2real", {}, {"nous-pin": "x@y"})
        self.assertIsNone(e["pin"])

    def test_broken_checkout_cannot_read_sha_but_is_found(self):
        repo = make_repo(self.tmp / "broken")
        (repo / ".git/HEAD").write_text("garbage\n")
        e = self.engine("nous", {"engines": {"nous": str(repo)}},
                        {"nous-pin": "main@0000000"})
        self.assertTrue(e["found"])
        self.assertEqual(e["warnings"],
                         ["can't read the installed sha to compare with pin main@0000000"])

    def test_path_source_without_dist_name(self):
        with mock.patch.object(doctor.shutil, "which", return_value="/bin/sim2real"):
            e = self.engine("sim2real", {})
        self.assertTrue(e["found"])
        self.assertEqual(e["source"], "PATH")
        self.assertIsNone(e["sha"])

    def test_configured_path_uses_expanduser(self):
        home = self.tmp / "home"
        home.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            (home / "eng").mkdir()
            e = self.engine("sim2real", {"engines": {"sim2real": "~/eng"}})
        self.assertTrue(e["found"])

    def test_ls_remote_failure_leaves_latest_unchecked(self):
        eng = make_repo(self.tmp / "eng", {"origin": "git@x:org/eng.git"})
        with mock.patch.object(doctor, "latest_release", return_value=None):
            e = self.engine("nous", {"engines": {"nous": str(eng)}}, offline=False)
        self.assertIsNone(e["latest_release"])
        self.assertFalse(e["latest_checked"])

    def test_tagless_remote_reports_no_latest(self):
        eng = make_repo(self.tmp / "eng", {"origin": "git@x:org/eng.git"})
        with mock.patch.object(doctor, "latest_release",
                               return_value={"tag": None, "sha": None}):
            e = self.engine("nous", {"engines": {"nous": str(eng)}}, offline=False)
        self.assertEqual(e["latest_release"], {"tag": None, "sha": None})
        self.assertTrue(e["latest_checked"])

    def test_branch_pin_is_not_compared_to_release_tags(self):
        eng = make_repo(self.tmp / "eng", {"origin": "git@x:org/eng.git"})
        with mock.patch.object(doctor, "latest_release",
                               return_value={"tag": "v2.0.0", "sha": "b" * 40}):
            e = self.engine("nous", {"engines": {"nous": str(eng)}},
                            {"nous-pin": "main@" + "0" * 7}, offline=False)
        self.assertFalse(any("is not the latest release" in w for w in e["warnings"]))


class TestStartEndTimeMethod(unittest.TestCase):
    def test_ladder(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        stat = tmp / "stat"
        stat.write_text("x")
        none = lambda _: None  # noqa: E731
        ps = lambda _: "/bin/ps"  # noqa: E731
        self.assertEqual(doctor.start_time_method("linux", stat, none), "proc-stat")
        self.assertEqual(doctor.start_time_method("linux", tmp / "no", ps), "ps-lstart")
        self.assertEqual(doctor.start_time_method("darwin", stat, ps), "ps-lstart")
        self.assertIsNone(doctor.start_time_method("darwin", stat, none))


if __name__ == "__main__":
    unittest.main()
