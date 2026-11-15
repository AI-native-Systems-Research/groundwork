"""Branch coverage for adapters/spotlights_static.py (91% on main -> ~100%).

The main suite (tests/test_runtime_spotlights_static.py) covers the happy
paths of every phase; this file covers the refusal, fallback and error
branches those paths never reach, measured on main 8d40c69 (CI run
43504194546, unit job 3.12) as 35 uncovered statements:

  - _default_probe's except (unparseable machine config -> the engine record
    is still built from doctor's defaults), _default_dry_run (the real
    subprocess runner) and _load's unreadable/invalid-JSON fallback
  - versions()/probe error paths and every remaining _semantic_problems
    refusal (non-dict models, unknown model key, invalid model id, duplicate
    includes, bad hints, blank repo_url, bad secrets)
  - _engine_problems: engine configured as a checkout, no pin sha;
    _include_problems when the target tree cannot be listed
  - dry_run's OSError face, the manager-dir manifest fallback, the
    num_candidates reconciliation warnings, the "no module was analysed"
    failure, spend()'s non-dict cost entry, cancel(interrupt=True)
  - collect()'s failure face: a study dir that is not a directory

Never runs spotlights-engine: the engine probe and the dry-run runner are
injected, exactly like the main suite.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_runtime_spotlights_static import CLAUDE, CODEX, Base  # noqa: E402

from ains_runtime import adapters as A  # noqa: E402
from ains_runtime import preparation as P  # noqa: E402
from ains_runtime.adapters import spotlights_static as SS  # noqa: E402


class ModuleHelpers(unittest.TestCase):
    """The module-level helpers the injected runners bypass."""

    def test_default_probe_survives_an_unparseable_machine_config(self):
        # A broken machine config must not crash the probe: it degrades to
        # doctor's defaults (the same offline record doctor itself reports).
        import _config
        with mock.patch.object(_config, "load_machine_config",
                               side_effect=_config.ConfigError("bad yaml")):
            p = SS._default_probe(None)
        self.assertEqual(set(p), {"found", "source", "path", "sha", "pin"})
        self.assertFalse(p["found"])

    def test_default_dry_run_runs_the_real_subprocess(self):
        # _default_dry_run is what the shipped adapter uses when no runner is
        # injected: a real subprocess.run with the timeout, stdout and stderr
        # returned as-is.
        rc, so, se = SS._default_dry_run(
            [sys.executable, "-c",
             "import sys; print('out'); print('err', file=sys.stderr)"],
            cwd=".", env={})
        self.assertEqual(rc, 0)
        self.assertIn("out", so)
        self.assertIn("err", se)

    def test_load_returns_none_for_missing_or_invalid_json(self):
        with mock.patch("pathlib.Path.read_text",
                        side_effect=OSError("unreadable")):
            self.assertIsNone(SS._load(Path("/nope/result.json")))
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "result.json"
            p.write_text("{not json")
            self.assertIsNone(SS._load(p))


class Refusals(Base):
    """validate()'s refusal branches the happy paths never reach."""

    def problems(self, sem_over):
        # _semantic_problems directly (req() merges semantics, so a key the
        # request must lack would be re-added by the merge)
        body = self.req()["body"]
        sem = dict(body["semantics"])
        sem.update(sem_over)
        sem.pop("models", None) if sem_over.get("models") is None else None
        return " | ".join(self.adapter._semantic_problems({**body,
                                                           "semantics": sem}))

    def test_semantic_refusals(self):
        cases = {
            "models not a dict": ({"models": None},
                                  "semantics.models: required {claude, codex}"),
            "unknown model key": ({"models": {"claude": CLAUDE, "codex": CODEX,
                                              "sonnet": "x"}},
                                  "semantics.models.sonnet: unknown (only claude, codex)"),
            "invalid model id": ({"models": {"claude": "Not A Model!",
                                             "codex": CODEX}},
                                 "is not a valid model id"),
            "duplicate includes": ({"include": ["toy/cache", "toy/cache"]},
                                   "semantics.include: duplicate names"),
            "bad hints": ({"hints": ["ok", "  "]},
                          "semantics.hints: a list of non-blank strings"),
            "blank repo_url": ({"repo_url": "   "},
                               "semantics.repo_url: a non-blank string"),
            "bad secrets": ({"secrets": ["9bad"]},
                            "semantics.secrets: a list of environment variable names"),
        }
        for name, (over, want) in cases.items():
            with self.subTest(name):
                self.assertIn(want, self.problems(over))

    def test_engine_problems_checkout_and_missing_pin(self):
        # source != PATH: the runtime launches only the installed executable
        self.probe.update(source="checkout", path=str(self.tmp / "engine-checkout"))
        self.assertIn("configured as a checkout", " ".join(
            self.adapter._engine_problems(None)))
        # no pin sha in the probe record and none in lock.json's pin key
        with mock.patch("doctor.read_pins", return_value={}):
            self.probe.update(source="PATH", pin=None)
            self.assertIn("no spotlights-pin sha", " ".join(
                self.adapter._engine_problems(None)))

    def test_versions_raises_when_engine_missing(self):
        res = self.prepare()                       # probe still healthy here
        self.probe.update(found=False)
        with self.assertRaisesRegex(SS.SpotlightsStaticError, "not found"):
            self.adapter.versions(self.ctx(self.record(res)))

    def test_probe_error_is_found_false_with_the_reason(self):
        def boom(ctx):
            raise RuntimeError("engine dir unreadable")
        self.adapter.engine_probe = boom
        p = self.adapter._probe(None)
        self.assertFalse(p["found"])
        self.assertIn("engine dir unreadable", p["error"])
        self.assertIn("engine dir unreadable", self.refused())

    def test_include_check_when_the_tree_cannot_be_listed(self):
        # git ls-tree failing (e.g. a target that is not a git repo) must
        # report "can't list", not crash
        rec = self.record(self.prepare())
        not_a_repo = self.tmp / "not-a-repo"
        not_a_repo.mkdir()
        self.assertIsNone(SS.SpotlightsStaticAdapter.module_paths_at(
            not_a_repo, self.commit))
        probs = self.adapter._include_problems(
            {**self.req(), "target": {"commit": self.commit, "repo_name": "x"}},
            A.AdapterContext(target=not_a_repo, runs_dir=self.runs,
                             prep_dir=self.tmp / "p", env=self.env))
        self.assertIn("can't list the target tree", " ".join(probs))

    def test_module_paths_ignores_blank_ls_tree_lines(self):
        # ls-tree output with blank lines must not crash the normalizer
        import _config
        with mock.patch.object(_config, "git", return_value="\n\n"):
            self.assertEqual(
                SS.SpotlightsStaticAdapter.module_paths_at(self.repo, self.commit),
                set())

    def test_dry_run_runner_raising_is_a_prepare_refusal(self):
        def boom(argv, cwd, env):
            raise OSError("no such file or directory: spotlights-engine")
        self.adapter.dry_run_runner = boom
        self.assertIn("--dry-run could not run", self.refused())


class AssessBranches(Base):
    """assess()/classify() branches the five fixtures never reach."""

    def setUp(self):
        super().setUp()
        self.rec = self.record(self.prepare())

    def classify(self, fixture=None, rc=0, cancel=False):
        if fixture:
            self.place(self.rec, fixture)
        inv = {"exit_code": rc, "cancel_requested_at": "t" if cancel else None}
        return self.adapter.classify(inv, self.rec, self.ctx(self.rec))

    def edit_manifest(self, mutate):
        out = Path(self.rec["non_material"]["output_folder"])
        m = json.loads((out / "run_manifest.json").read_text())
        mutate(m)
        (out / "run_manifest.json").write_text(json.dumps(m))
        return out

    def test_manifest_in_the_manager_dir_is_found(self):
        # The engine can write run_manifest.json under the artifacts dir's
        # spotlights_manager/ when the output dir is not writable; assess must
        # still find it (the manifest_p fallback).
        self.place(self.rec, "candidates-found")
        out = Path(self.rec["non_material"]["output_folder"])
        art = Path(self.rec["non_material"]["artifacts_dir"])
        mgr_dir = art / "spotlights_manager"
        mgr_dir.mkdir(parents=True)
        (out / "run_manifest.json").rename(mgr_dir / "run_manifest.json")
        ns = self.classify()
        self.assertEqual(ns.state, "completed")
        self.assertTrue(any(e["kind"] == "spotlights-run-manifest"
                            for e in ns.evidence))

    def test_num_candidates_reconciled_with_result_json(self):
        # missing in the manifest: counted from result.json, with a warning
        self.place(self.rec, "candidates-found")
        self.edit_manifest(lambda m: m["outputs"].pop("num_candidates"))
        ns = self.classify()
        self.assertEqual(ns.state, "completed")
        self.assertTrue(any("num_candidates missing" in w for w in ns.warnings))
        # present but disagreeing: both numbers reported in a warning
        self.edit_manifest(lambda m: m["outputs"].__setitem__("num_candidates", 99))
        ns = self.classify()
        self.assertTrue(any("run_manifest says 99" in w for w in ns.warnings))

    def test_every_module_skipped_is_a_failure(self):
        # include matched nothing (or every module SKIPPED): not a
        # no_opportunity, a failure
        self.place(self.rec, "candidates-found")
        out = Path(self.rec["non_material"]["output_folder"])
        r = json.loads((out / "result.json").read_text())
        for mr in r["module_runs"].values():
            mr["status"] = "SKIPPED"
        (out / "result.json").write_text(json.dumps(r))
        self.edit_manifest(lambda m: m["outputs"].__setitem__("num_candidates", 0))
        ns = self.classify()
        self.assertEqual(ns.state, "failed")
        self.assertIn("no module was analysed", ns.reason)

    def test_evidence_delegates_to_the_base_adapter(self):
        from ains_runtime.adapters.base import Adapter
        self.place(self.rec, "candidates-found")
        inv = {"exit_code": 0, "cancel_requested_at": None}
        ctx = self.ctx(self.rec)
        self.assertEqual(self.adapter.evidence(inv, self.rec, ctx),
                         Adapter.evidence(self.adapter, inv, self.rec, ctx))

    def test_cancel_interrupt_note(self):
        # --interrupt has no separate meaning for this capability: the note
        # says so instead of silently ignoring the flag
        res = self.adapter.cancel({}, self.rec, self.ctx(self.rec), interrupt=True)
        self.assertIn("--interrupt has no separate meaning", res["note"])
        res = self.adapter.cancel({}, self.rec, self.ctx(self.rec), interrupt=False)
        self.assertNotIn("--interrupt", res["note"])

    def test_spend_ignores_a_non_dict_cost_entry(self):
        # external_cost must be a dict to contribute; a non-dict entry is
        # skipped without losing the priced_token_share completeness flag
        manifest = {"cost": {"amount_usd": 1.5, "priced_token_share": 1.0},
                    "external_cost": "not-a-dict"}
        s = SS.SpotlightsStaticAdapter.spend(manifest)
        self.assertEqual(s["reported_usd"], 1.5)
        self.assertEqual(s["completeness"], "complete")
        self.assertEqual(SS.SpotlightsStaticAdapter.spend(None)["completeness"],
                         "unknown")


class CollectFailure(Base):
    """collect()'s failure face: a study dir that is not a directory."""

    def setUp(self):
        super().setUp()
        self.rec = self.record(self.prepare())
        self.study = self.repo / "investigations" / "s1"
        self.study.mkdir(parents=True)

    def test_collect_failure_is_reported_not_raised(self):
        self.place(self.rec, "candidates-found")
        ctx = self.ctx(self.rec)
        ctx.study = self.tmp / "no-such-study"    # collect_run raises CouldNotRun
        inv = {"exit_code": 0, "cancel_requested_at": None}
        r = self.adapter.collect(inv, self.rec, ctx)
        self.assertEqual((r["outcome"], r["collection"]["state"]),
                         ("candidates_found", "failed"))
        self.assertIn("is not a directory", r["collection"]["error"])
        self.assertIsNone(r["collection"]["path"])
        self.assertFalse((self.study / "collection.json").exists())
