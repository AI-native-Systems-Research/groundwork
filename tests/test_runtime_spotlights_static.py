"""R5: Spotlights static adapter (plan §5 R5; §1 Q3-Q5; G3, G8, G9, G11; §4.4).

Never runs spotlights-engine (installed or not): the engine probe and the
--dry-run runner are injected, and the one end-to-end launch uses a fake
engine script written into the test's temp dir. Native-output fixtures are
in tests/fixtures/runtime/spotlights_static/ (derived from the collector's
tests/fixtures/spotlights/run-a/).
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cov_child_env import covsite_env  # noqa: E402
from _runtime_util import kill_matching, request, wait_for  # noqa: E402
from ains_runtime import adapters as A  # noqa: E402
from ains_runtime import approval as AP  # noqa: E402
from ains_runtime import preparation as P  # noqa: E402
from ains_runtime import procid  # noqa: E402
from ains_runtime import recovery as R  # noqa: E402
from ains_runtime import store as S  # noqa: E402
from ains_runtime import supervisor as SV  # noqa: E402
from ains_runtime.adapters import spotlights_static as SS  # noqa: E402
from test_runtime_prepare import git, go  # noqa: E402

import collect_spotlights as CS  # noqa: E402

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures" / "runtime" / "spotlights_static"
ENGINES = HERE / "fixtures" / "runtime" / "fake_engines"
PIN_SHA = "cccc290f8dec4106fb940184b999b4b1180faf09"
CLAUDE, CODEX = "claude-test-model", "gpt-test-model"

CONSTANTS = "# toy constants\n\nBLOCK_SIZE = 16\nMAX_BLOCKS = 4096\n# 18%\n"
POLICY = ("class LRUPolicy:\n    \"\"\"Toy policy.\"\"\"\n\n"
          "    def evict(self, n):\n        out = []\n        for k in list(self.order):\n"
          "            out.append(k)\n        return out[:n]\n\n\n"
          "    def touch(self, k):\n        self.order.move_to_end(k)\n")

FAKE_ENGINE = """#!{py}
# Test-only stand-in for spotlights-engine: copies a fixture run into
# --output-folder, creates --artifacts-dir, exits with FAKE_SPOTLIGHTS_RC.
import os, shutil, sys
a = sys.argv[1:]
out, art = a[a.index("--output-folder") + 1], a[a.index("--artifacts-dir") + 1]
os.makedirs(art)
fx = os.environ.get("FAKE_SPOTLIGHTS_FIXTURE")
if fx:
    shutil.copytree(fx, out, dirs_exist_ok=True)
sys.exit(int(os.environ.get("FAKE_SPOTLIGHTS_RC", "0")))
"""


def dry_ok(calls=None, claude=CLAUDE, codex=CODEX, rc=0):
    def run(argv, cwd, env):
        if calls is not None:
            calls.append({"argv": list(argv), "cwd": cwd, "env": dict(env)})
        a = argv
        for flag in ("--output-folder", "--artifacts-dir"):   # the engine mkdirs both
            os.makedirs(a[a.index(flag) + 1], exist_ok=True)
        out = ("dry-run: no agents invoked, no cost incurred.\n"
               f"  repo:        {a[a.index('--repo') + 1]}\n"
               "  scope:       toy/cache\n"
               f"  claude-model:{claude}\n  codex-model: {codex}\n")
        return rc, out, "spotlights-engine: bad models file" if rc else ""
    return run


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(self._cleanup)
        self.repo = self.tmp / "demo-repo"
        (self.repo / "toy/cache").mkdir(parents=True)
        (self.repo / "toy/cache/constants.py").write_text(CONSTANTS)
        (self.repo / "toy/cache/policy.py").write_text(POLICY)
        (self.repo / "Toy-Utils").mkdir()
        (self.repo / "Toy-Utils/x.py").write_text("x = 1\n")
        git(self.tmp, "init", "-q", "-b", "main", str(self.repo))
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "init")
        self.commit = git(self.repo, "rev-parse", "HEAD")
        self.db = self.tmp / "runs" / "demo-repo" / "s1" / ".runtime" / S.DB_NAME
        self.store = S.open_store(self.db)
        self.exe = str(self.tmp / "bin" / "spotlights-engine")
        self.probe = {"found": True, "source": "PATH", "path": self.exe,
                      "sha": PIN_SHA, "pin": "v0.1.1@cccc290"}
        self.dry_calls = []
        self.adapter = SS.SpotlightsStaticAdapter(
            engine_probe=lambda ctx: dict(self.probe),
            dry_run_runner=dry_ok(self.dry_calls), cancel_grace=1.0)
        prev = A.register(self.adapter, replace=True)
        self.addCleanup(A.unregister, SS.CAPABILITY, prev)
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.tmp),
                    "CLAUDE_CODE_ENTRYPOINT": "cli",
                    **{k: v for k, v in os.environ.items()
                     if k.startswith("COVERAGE_")},
                    **covsite_env()}
        self.n = 0

    def _cleanup(self):
        kill_matching(self.tmp.name)
        SV.reap(timeout=5)
        self.store.close()
        P.make_writable(P.prepared_root(self.store))
        shutil.rmtree(self.tmp, ignore_errors=True)

    @property
    def runs(self):
        return self.db.parent.parent

    def req(self, **over):
        self.n += 1
        sem = {"include": ["toy/cache"], "models": {"claude": CLAUDE, "codex": CODEX},
               "hints": ["multi-turn chat"]}
        sem.update(over.pop("semantics", {}))
        body = dict(capability="spotlights-static", task_type="explain",
                    objective="reduce median TTFT", baseline=None, hypothesis=None,
                    limits={"max_cost_usd": 20}, semantics=sem)
        body.update(over)
        m = request(f"req-{self.n}", work_id=f"w-{self.n}", **body)
        m["target"]["commit"] = self.commit
        return m

    def submit(self, msg):
        res = self.store.submit(msg, env=self.env)
        self.assertTrue(res.ok, res.reasons)
        return msg["work_id"]

    def prepare(self, msg=None):
        wid = self.submit(msg or self.req())
        return P.prepare(self.store, wid, target=self.repo, env=self.env)

    def refused(self, msg=None):
        wid = self.submit(msg or self.req())
        with self.assertRaises(P.PrepareError) as cm:
            P.prepare(self.store, wid, target=self.repo, env=self.env)
        return " | ".join(cm.exception.reasons)

    def record(self, res):
        return self.store.get_preparation(res.prep_id)["record"]

    def ctx(self, rec):
        return A.AdapterContext(target=Path(rec["target_path"]), runs_dir=self.runs,
                                prep_dir=P.prep_dir(self.store, rec["prep_id"]),
                                env=self.env, repo_name="demo-repo", work_id=rec["work_id"])

    def place(self, rec, fixture, sha=None):
        """Put a fixture run where the prepared invocation writes its output."""
        out, art = Path(rec["non_material"]["output_folder"]), Path(
            rec["non_material"]["artifacts_dir"])
        shutil.copytree(FIX / fixture, out)
        art.mkdir()
        mp = out / "run_manifest.json"
        m = json.loads(mp.read_text())
        m["target"]["commit_sha"] = sha or self.commit
        mp.write_text(json.dumps(m))
        return out


# --- validate / prepare --------------------------------------------------------

class Prepare(Base):
    def test_argv_explicit_models_fresh_dirs_no_resume(self):
        res = self.prepare()
        rec = self.record(res)
        argv = rec["argv"]
        out = self.runs / f"spotlights-{res.prep_id}"
        self.assertEqual(argv[:5], [self.exe, "--repo", str(self.repo), "--include",
                                    "toy/cache"])
        self.assertIn(f"--claude-model={CLAUDE}", argv)
        self.assertIn(f"--codex-model={CODEX}", argv)
        self.assertIn("--objective=reduce median TTFT", argv)
        self.assertIn("--hint=multi-turn chat", argv)
        self.assertIn("--max-cost=20.0", argv)
        self.assertEqual(argv[argv.index("--output-folder") + 1], str(out))
        self.assertEqual(argv[argv.index("--artifacts-dir") + 1], str(out / "artifacts"))
        self.assertEqual(argv[-1], "--no-resume")
        self.assertNotIn("--dry-run", argv)
        self.assertFalse(out.exists())            # prepare creates no run dir
        self.assertEqual(rec["native_run_id"], f"spotlights-{res.prep_id}")
        # Placeholders in the material copy, not paths.
        margv = rec["material"]["invocation"]["argv"]
        self.assertIn("{output_folder}", margv)
        self.assertIn("{artifacts_dir}", margv)

    def test_max_cost_is_advisory(self):
        rec = self.record(self.prepare())
        lim = {x["name"]: x for x in rec["material"]["limits"]}
        self.assertEqual(lim["max_cost_usd"]["enforced_by"], "advisory")
        self.assertEqual(lim["max_cost_usd"]["value"], 20)
        from ains_runtime import limits as L
        self.assertEqual(L.launch_problems(rec), [])
        card = (P.prep_dir(self.store, rec["prep_id"]) / "card.md").read_text()
        self.assertIn("max_cost_usd (advisory)", card)

    def test_dry_run_preflight_uses_deleted_temp_dirs(self):
        res = self.prepare()
        rec = self.record(res)
        self.assertEqual(len(self.dry_calls), 1)
        dry = self.dry_calls[0]["argv"]
        self.assertIn("--dry-run", dry)
        self.assertNotIn("--no-resume", dry)
        real_out = rec["non_material"]["output_folder"]
        self.assertNotIn(real_out, " ".join(dry))
        for flag in ("--output-folder", "--artifacts-dir"):
            p = Path(dry[dry.index(flag) + 1])
            self.assertFalse(p.exists(), p)        # temp dir deleted afterwards
            self.assertFalse(str(p).startswith(str(self.repo)))
        self.assertEqual(rec["material"]["adapter_material"]["dry_run"],
                         {"claude_model": CLAUDE, "codex_model": CODEX, "scope": "toy/cache"})
        self.assertNotIn("UNRELATED", self.dry_calls[0]["env"])

    def test_dry_run_failure_or_model_mismatch_refuses(self):
        self.adapter.dry_run_runner = dry_ok(rc=2)
        self.assertIn("--dry-run failed (exit 2)", self.refused())
        self.adapter.dry_run_runner = dry_ok(claude="(CLI default)")
        self.assertIn("resolved other models", self.refused())
        self.assertEqual(self.store.list_preparations(), [])

    def test_required_semantics(self):
        cases = {
            "models": (dict(semantics={"models": {"claude": CLAUDE}}),
                       "semantics.models.codex: required"),
            "include missing": (dict(semantics={"include": []}), "semantics.include: required"),
            "include form": (dict(semantics={"include": ["Toy-Utils"]}),
                             "normalized: toy_utils"),
            "max cost": (dict(limits={"max_cost_usd": 0}), "limits.max_cost_usd"),
        }
        for name, (over, want) in cases.items():
            with self.subTest(name):
                self.assertIn(want, self.refused(self.req(**over)))
        # contracts already refuse a blank objective; the adapter checks too
        body = self.req()["body"]
        self.assertIn("objective: required (non-blank)",
                      " ".join(self.adapter._semantic_problems({**body, "objective": " "})))

    def test_include_must_exist_at_the_commit(self):
        self.assertIn("'toy/nope' matches no directory",
                      self.refused(self.req(semantics={"include": ["toy/nope"]})))
        # normalized segment names and source-root-relative suffixes are found
        self.prepare(self.req(semantics={"include": ["toy_utils", "cache"]}))
        # a module added after the request commit is not at the commit
        (self.repo / "late").mkdir()
        (self.repo / "late/a.py").write_text("a\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "late")
        names = SS.SpotlightsStaticAdapter.module_paths_at(self.repo, self.commit)
        self.assertNotIn("late", names)

    def test_engine_sha_must_equal_pin(self):
        self.probe["sha"] = "d" * 40
        self.assertIn("is not the pin", self.refused())
        self.probe["sha"] = None
        self.assertIn("can't read the installed", self.refused())
        self.probe.update(found=False)
        self.assertIn("spotlights-engine not found", self.refused())

    def test_artifacts_dir_existing_or_inside_repo_refused(self):
        msg = self.req()
        wid = self.submit(msg)
        with mock.patch.object(P, "new_prep_id", return_value="prep-0123456789ab"):
            (self.runs / "spotlights-prep-0123456789ab" / "artifacts").mkdir(parents=True)
            with self.assertRaises(P.PrepareError) as cm:
                P.prepare(self.store, wid, target=self.repo, env=self.env)
        self.assertIn("already exists", " ".join(cm.exception.reasons))
        ctx = A.AdapterContext(target=self.repo, runs_dir=self.repo / "runs",
                               prep_dir=self.tmp / "p" / "prep-x", env=self.env)
        probs = self.adapter._dir_problems(ctx)
        self.assertTrue(any("inside the target repo" in p for p in probs), probs)

    def test_equivalent_requests_same_digest(self):
        a, b = self.prepare(), self.prepare()
        self.assertNotEqual(self.record(a)["argv"], self.record(b)["argv"])
        self.assertEqual(a.material_digest, b.material_digest)

    def test_each_material_field_changes_the_digest(self):
        base = self.prepare().material_digest
        muts = {
            "include": dict(semantics={"include": ["toy"]}),
            "claude": dict(semantics={"models": {"claude": "other", "codex": CODEX}}),
            "codex": dict(semantics={"models": {"claude": CLAUDE, "codex": "other"}}),
            "hints": dict(semantics={"hints": ["batch"]}),
            "repo_url": dict(semantics={"repo_url": "https://example.com/demo.git"}),
            "secrets": dict(semantics={"secrets": ["OPENAI_API_KEY"]}),
            "objective": dict(objective="maximize throughput"),
            "max_cost": dict(limits={"max_cost_usd": 5}),
        }
        for name, over in muts.items():
            with self.subTest(name):
                if name in ("claude", "codex"):
                    m = over["semantics"]["models"]
                    self.adapter.dry_run_runner = dry_ok(claude=m["claude"], codex=m["codex"])
                self.assertNotEqual(self.prepare(self.req(**over)).material_digest, base)
                self.adapter.dry_run_runner = dry_ok()
        with self.subTest("SPOTLIGHTS_MODELS_FILE"):
            f = self.tmp / "models.yaml"
            f.write_text("claude: x\n")
            self.env["SPOTLIGHTS_MODELS_FILE"] = str(f)
            self.assertNotEqual(self.prepare().material_digest, base)
            del self.env["SPOTLIGHTS_MODELS_FILE"]
        with self.subTest("~/.claude/settings.json digest"):
            (self.tmp / ".claude").mkdir()
            (self.tmp / ".claude/settings.json").write_text("{}")
            self.assertNotEqual(self.prepare().material_digest, base)
            shutil.rmtree(self.tmp / ".claude")
        with self.subTest("engine sha"):
            self.adapter.engine_probe = lambda ctx: {**self.probe, "sha": PIN_SHA[:7] + "0" * 33}
            d = self.prepare().material_digest
            self.adapter.engine_probe = lambda ctx: dict(self.probe)
            self.assertNotEqual(d, base)
        self.assertEqual(self.prepare().material_digest, base)

    def test_material_mutation_rejected_after_approval(self):
        res = self.prepare()
        AP.approve(self.store, res.prep_id, code=res.short_code, source="chat",
                   quote=go(res.short_code), env=self.env)
        self.assertTrue(P.revalidate(self.store, res.prep_id, env=self.env).ok)
        cases = {
            "models file": lambda: self.env.__setitem__("SPOTLIGHTS_MODELS_FILE", "/x"),
            "engine sha": lambda: self.probe.__setitem__("sha", "d" * 40),
            "dry-run models": lambda: setattr(self.adapter, "dry_run_runner",
                                              dry_ok(codex="gpt-other")),
            "codex config": lambda: ((self.tmp / ".codex").mkdir(exist_ok=True),
                                     (self.tmp / ".codex/config.toml").write_text("m='x'")),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                saved = (dict(self.env), dict(self.probe), self.adapter.dry_run_runner)
                mutate()
                rv = P.revalidate(self.store, res.prep_id, env=self.env)
                self.assertFalse(rv.ok, name)
                self.env.clear()
                self.env.update(saved[0])
                self.probe.clear()
                self.probe.update(saved[1])
                self.adapter.dry_run_runner = saved[2]
                shutil.rmtree(self.tmp / ".codex", ignore_errors=True)
        # The output dir appearing before launch also blocks (G9).
        Path(self.record(res)["non_material"]["artifacts_dir"]).mkdir(parents=True)
        rv = P.revalidate(self.store, res.prep_id, env=self.env)
        self.assertTrue(any("already exists" in p for p in rv.problems), rv.problems)

    def test_execute_refuses_a_non_fresh_argv(self):
        rec = self.record(self.prepare())
        spec = self.adapter.execute(rec, self.ctx(rec))
        self.assertEqual(spec.argv, rec["argv"])
        bad = dict(rec, argv=[a for a in rec["argv"] if a != "--no-resume"])
        with self.assertRaises(SS.SpotlightsStaticError):
            self.adapter.execute(bad, self.ctx(rec))

    def test_shipped_registration_and_default_probe_is_offline(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]);"
                "from ains_runtime import adapters as A;"
                "print(type(A.get('spotlights-static')).__name__)")
        r = subprocess.run([sys.executable, "-c", code, str(HERE.parent /
                            "skills/investigation/scripts")], capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "SpotlightsStaticAdapter", r.stderr)
        # The default probe reads doctor's file-based engine record, offline.
        with mock.patch("doctor.latest_release", side_effect=AssertionError("network")):
            p = SS._default_probe(None)
        self.assertEqual(set(p), {"found", "source", "path", "sha", "pin"})


# --- classify (Q4, G8) ---------------------------------------------------------

class Classify(Base):
    def setUp(self):
        super().setUp()
        self.rec = self.record(self.prepare())

    def classify(self, fixture=None, rc=0, cancel=False):
        if fixture:
            self.place(self.rec, fixture)
        inv = {"exit_code": rc, "cancel_requested_at": "t" if cancel else None}
        return self.adapter.classify(inv, self.rec, self.ctx(self.rec))

    def test_candidates_found(self):
        ns = self.classify("candidates-found")
        self.assertEqual((ns.state, ns.native_stop_reason, ns.partial),
                         ("completed", "candidates_found", False))
        kinds = {e["kind"] for e in ns.evidence}
        self.assertTrue({"spotlights-result", "spotlights-run-manifest"} <= kinds)

    def test_partial_when_degraded(self):
        ns = self.classify("partial-degraded")
        self.assertEqual((ns.state, ns.native_stop_reason, ns.partial),
                         ("completed", "candidates_found", True))
        self.assertIn("DEGRADED toy/cache", ns.reason)

    def test_no_opportunity_vs_failed(self):
        ns = self.classify("no-opportunity")
        self.assertEqual((ns.state, ns.native_stop_reason), ("completed", "no_opportunity"))
        shutil.rmtree(self.rec["non_material"]["output_folder"])
        ns = self.classify("failed-unrecoverable", rc=1)
        self.assertEqual(ns.state, "failed")
        self.assertIsNone(ns.native_stop_reason)
        self.assertIn("exit 1", ns.reason)

    def test_failed_rules(self):
        cases = [("failed-unrecoverable", 0, "recoverable=False"),
                 ("zero-degraded", 0, "not a clean no_opportunity"),
                 ("candidates-found", 2, "exit 2"),
                 ("candidates-found", 1, "exit 1"),
                 (None, 0, "result.json missing")]
        for fixture, rc, want in cases:
            with self.subTest(f"{fixture} rc={rc}"):
                shutil.rmtree(self.rec["non_material"]["output_folder"], ignore_errors=True)
                ns = self.classify(fixture, rc=rc)
                self.assertEqual(ns.state, "failed")
                self.assertIn(want, ns.reason)

    def test_failed_module_and_missing_manifest(self):
        out = self.place(self.rec, "candidates-found")
        r = json.loads((out / "result.json").read_text())
        r["module_runs"]["toy/cache"]["status"] = "FAILED"
        (out / "result.json").write_text(json.dumps(r))
        ns = self.classify()
        self.assertIn("FAILED: toy/cache", ns.reason)
        (out / "run_manifest.json").unlink()
        self.assertIn("run_manifest.json missing", self.classify().reason)

    def test_exit_code_alone_never_completes(self):
        ns = self.classify(rc=0)
        self.assertEqual(ns.state, "failed")

    def test_cancelled_without_result(self):
        Path(self.rec["non_material"]["artifacts_dir"]).mkdir(parents=True)
        ns = self.classify(rc=-15, cancel=True)
        self.assertEqual((ns.state, ns.partial), ("cancelled", True))
        self.assertIn("no final report", ns.reason)
        # killed with no cancel request is a failure
        self.assertEqual(self.classify(rc=-9).state, "failed")

    def test_cancel_requested_but_finished_reports_real_state(self):
        ns = self.classify("no-opportunity", cancel=True)
        self.assertEqual(ns.state, "completed")
        self.assertTrue(any("finished first" in w for w in ns.warnings))

    def test_lost_exit_code_classified_from_evidence_with_warning(self):
        ns = self.classify("candidates-found", rc=None)
        self.assertEqual(ns.state, "completed")
        self.assertTrue(any("exit code unknown" in w for w in ns.warnings))

    def test_commit_mismatch_warns(self):
        self.place(self.rec, "candidates-found", sha="f" * 40)
        ns = self.classify()
        self.assertTrue(any("not the prepared commit" in w for w in ns.warnings))


# --- collect ----------------------------------------------------------------------

class Collect(Base):
    def setUp(self):
        super().setUp()
        self.rec = self.record(self.prepare())
        self.study = self.repo / "investigations" / "s1"
        self.study.mkdir(parents=True)

    def collect(self, fixture, rc=0, cancel=False):
        if fixture:
            self.place(self.rec, fixture)
        inv = {"exit_code": rc, "cancel_requested_at": "t" if cancel else None}
        return self.adapter.collect(inv, self.rec, self.ctx(self.rec))

    def test_candidates_found_collects_citations(self):
        before = {str(p): p.stat().st_mtime_ns for p in
                  Path(self.rec["non_material"]["output_folder"]).rglob("*")} \
            if Path(self.rec["non_material"]["output_folder"]).exists() else None
        self.assertIsNone(before)
        r = self.collect("candidates-found")
        self.assertEqual((r["outcome"], r["partial"]), ("candidates_found", False))
        self.assertEqual(r["collection"]["state"], "collected")
        self.assertEqual(Path(r["collection"]["path"]), self.study / "collection.json")
        ids = [c["candidate_id"] for c in r["claims_draft"]]
        self.assertEqual(ids, ["cand-toy_cache-0001", "cand-toy_cache-0002",
                               "cand-toy_cache-0003"])
        for c in r["claims_draft"]:
            self.assertIn(c["value"], (self.study / c["evidence"]).read_text())
        self.assertEqual(r["estimate_guard"], {"checked": 3, "violations": []})
        self.assertEqual(r["spend"]["completeness"], "partial")   # no priced_token_share
        self.assertEqual(r["spend"]["reported_usd"], 2.861085)
        self.assertEqual(r["n_candidates"], 4)
        sec = json.loads((self.study / "collection.json").read_text())["spotlights"]
        self.assertEqual(sec["target"]["cited_sha"], self.commit)

    def test_estimated_impact_never_a_claim_value(self):
        r = self.collect("candidates-found")
        out = Path(self.rec["non_material"]["output_folder"])
        res = json.loads((out / "result.json").read_text())
        forbidden = CS.estimate_values(res["report"], None,
                                       json.loads((out / "run_manifest.json").read_text()))
        self.assertIn("high", forbidden)
        self.assertTrue(r["claims_draft"])
        for c in r["claims_draft"]:
            self.assertNotIn(c["value"], forbidden)
            for cand in res["report"]["candidates"]:
                self.assertNotEqual(c["value"], cand["estimated_impact"])
        for t in r["candidates_top"]:
            self.assertIn("spotlights_estimate_not_a_claim", t)

    def test_guard_rejects_an_estimate_claim(self):
        def leaky(study, run_dir, **kw):
            sec = {"claims_draft": [{"id": "cite-x", "value": "high"}], "notes": []}
            return sec, Path(study) / "collection.json"
        with mock.patch.object(CS, "collect_run", leaky):
            r = self.collect("candidates-found")
        self.assertEqual(r["collection"]["state"], "failed")
        self.assertEqual(r["estimate_guard"]["violations"], ["cite-x"])
        self.assertEqual(r["claims_draft"], [])

    def test_partial_is_labelled(self):
        r = self.collect("partial-degraded")
        self.assertEqual((r["outcome"], r["partial"]), ("candidates_found", True))
        texts = " ".join(x["text"] for x in r["limitations"])
        self.assertIn("module toy/cache DEGRADED", texts)
        self.assertIn("codex timed out", texts)

    def test_no_opportunity_is_a_completed_collection(self):
        r = self.collect("no-opportunity")
        self.assertEqual(r["outcome"], "no_opportunity")
        self.assertEqual(r["native"]["state"], "completed")
        self.assertEqual(r["collection"]["state"], "collected")
        self.assertEqual(r["claims_draft"], [])

    def test_failed_and_cancelled_are_not_collected(self):
        r = self.collect("failed-unrecoverable", rc=1)
        self.assertEqual((r["outcome"], r["collection"]["state"]), ("failed", "not_run"))
        self.assertEqual(r["claims_draft"], [])
        self.assertFalse((self.study / "collection.json").exists())
        self.assertTrue(any("not recoverable" in x["text"] for x in r["limitations"]))
        shutil.rmtree(self.rec["non_material"]["output_folder"])
        r = self.collect(None, rc=-15, cancel=True)
        self.assertEqual(r["outcome"], "cancelled")
        self.assertEqual(r["spend"]["completeness"], "unknown")

    def test_collect_is_read_only_over_native_output(self):
        self.place(self.rec, "candidates-found")
        out = Path(self.rec["non_material"]["output_folder"])
        snap = {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in out.rglob("*")}
        self.collect(None)
        self.collect(None)                       # retryable
        self.assertEqual(snap, {str(p): (p.stat().st_mtime_ns, p.stat().st_size)
                                for p in out.rglob("*")})


# --- cancel (§4.4) and an end-to-end launch with a fake engine -----------------------

class Exec(Base):
    def test_cancel_signals_every_group_including_new_session_child(self):
        env = {**os.environ, "FAKE_PARENT": str(self.tmp / "r0")}
        argv = [sys.executable, str(ENGINES / "new_session_child.py"), "run", "c",
                "--run-id", "t"]
        eng = subprocess.Popen(argv, env=env, start_new_session=True)
        self.addCleanup(lambda: (eng.kill(), eng.wait(timeout=10)))
        st_f = self.tmp / "r0" / "t" / "state.json"
        wait_for(st_f.exists, what="tree up")
        st = json.loads(st_f.read_text())
        kids = (st["same_group_child"], st["new_session_child"])
        inv = {"engine_pid": eng.pid, "engine_start": procid.start_time(eng.pid),
               "engine_argv": argv}
        res = self.adapter.cancel(inv, {}, None)
        self.assertEqual(res["mechanism"], "process-groups")
        self.assertTrue(res["requested"])
        self.assertIn(st["new_session_child"], res["groups"])
        self.assertIn(eng.pid, res["groups"])
        self.assertIn("no stop mechanism", res["note"])
        eng.wait(timeout=10)
        wait_for(lambda: not any(procid.identity(k) for k in kids), what="children gone")
        self.assertEqual(res["survivors"], [])

    def test_launch_classify_collect_end_to_end(self):
        exe = Path(self.exe)
        exe.parent.mkdir()
        exe.write_text(FAKE_ENGINE.format(py=sys.executable))
        exe.chmod(0o755)
        self.env.update(FAKE_SPOTLIGHTS_FIXTURE=str(FIX / "candidates-found"))
        res = self.prepare(self.req(semantics={"secrets": ["FAKE_SPOTLIGHTS_FIXTURE"]}))
        AP.approve(self.store, res.prep_id, code=res.short_code, source="chat",
                   quote=go(res.short_code), env=self.env)
        inv_id = SV.launch(self.store, res.prep_id, env=self.env).inv_id
        v = wait_for(lambda: (lambda s: s if s["terminal"] else None)(
            R.status(self.store, inv_id, env=self.env)), what="terminal")
        # The fixture manifest's commit is a placeholder: completed + warning.
        self.assertEqual((v["state"], v["exit_code"]), ("completed", 0))
        self.assertEqual(v["native"]["native_stop_reason"], "candidates_found")
        self.assertTrue(any("not the prepared commit" in w for w in v["native"]["warnings"]))
        inv = self.store.get_invocation(inv_id)
        rec = self.record(res)
        self.assertTrue(Path(rec["non_material"]["artifacts_dir"]).is_dir())
        (self.repo / "investigations" / "s1").mkdir(parents=True)
        out = self.adapter.collect(R._adapter_view(inv), rec, self.ctx(rec))
        self.assertEqual(out["collection"]["state"], "collected")
        self.assertEqual(len(out["claims_draft"]), 3)
        # A second launch of the same approval is refused.
        with self.assertRaises(SV.LaunchError):
            SV.launch(self.store, res.prep_id, env=self.env)


if __name__ == "__main__":
    unittest.main()
