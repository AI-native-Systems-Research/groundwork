"""R4: the Nous adapter (plan §5 R4, §1 Q2/Q4, §4.4).

Never runs Nous: availability, the engine sha, the process list and `nous
stop` go through the adapter's injectable hooks. The BLIS apparatus is a
throwaway dir (a fake binary + a git catalog) in a temp dir.
"""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runtime_util import request  # noqa: E402
from ains_runtime import adapters as A  # noqa: E402
from ains_runtime import approval as AP  # noqa: E402
from ains_runtime import preparation as P  # noqa: E402
from ains_runtime import procid  # noqa: E402
from ains_runtime import store as S  # noqa: E402
from ains_runtime.adapters import nous as N  # noqa: E402
from ains_runtime.adapters.base import AdapterContext  # noqa: E402
from test_runtime_prepare import git, go, make_repo  # noqa: E402

import collect as C  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
PIN = "nous-v2-fixes@e224cc1"
ENGINE_SHA = "e224cc1694941957083238359c2bfbe8688ad71b"
BINARY = b"#!/bin/sh\necho fake blis\n"


def make_apparatus(root: Path) -> tuple[Path, str, str]:
    (root / "bin").mkdir(parents=True)
    b = root / "bin" / "blis-test"
    b.write_bytes(BINARY)
    cat = root / "blis-catalog"
    cat.mkdir()
    git(cat, "init", "-q", "-b", "main")
    (cat / "c.yaml").write_text("x: 1\n")
    git(cat, "add", "c.yaml")
    git(cat, "commit", "-qm", "c")
    sha, commit = N.sha256_file(b), git(cat, "rev-parse", "HEAD")
    lock_test_apparatus(sha, commit)
    return b, sha, commit


def lock_test_apparatus(sha: str, commit: str) -> None:
    """Add the fake binary and catalog to the lock for this test process only
    (lock.json itself is never edited by tests)."""
    import upstreams as U
    U._TEST_BLIS_BINARIES["test-fake"] = {"file": "bin/blis-test", "sha256": sha}
    U._TEST_CATALOG_COMMITS.add(commit)


class FakeHooks:
    def __init__(self, apparatus_root):
        self.probe_result = {"available": True, "reason": None, "engine_sha": ENGINE_SHA,
                             "pin": PIN, "pin_sha": "e224cc1",
                             "apparatus_root": str(apparatus_root), "nous_on_path": True}
        self.processes: list = []
        self.cmds: list = []
        self.rc = 0

    def probe(self, ctx):
        return dict(self.probe_result)

    def list_processes(self):
        return list(self.processes)

    def run_cmd(self, argv, env):
        self.cmds.append((list(argv), dict(env)))
        return self.rc, "Wrote STOP sentinel"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        self.repo = make_repo(self.tmp)
        self.commit = git(self.repo, "rev-parse", "HEAD")
        self.app = self.tmp / "apparatus"
        self.binary, self.bsha, self.cat_commit = make_apparatus(self.app)
        self.hooks = FakeHooks(self.app)
        self.adapter = N.NousAdapter(probe=self.hooks.probe,
                                     list_processes=self.hooks.list_processes,
                                     run_cmd=self.hooks.run_cmd)
        prev = A.register(self.adapter, replace=True)
        self.addCleanup(lambda: A.unregister("nous", prev))
        self.db = self.tmp / "runs" / "demo-repo" / "s1" / ".runtime" / S.DB_NAME
        self.store = S.open_store(self.db)
        self.env = {"HOME": str(self.tmp), "PATH": "/usr/bin:/bin"}
        self.n = 0

    def _cleanup(self):
        self.store.close()
        P.make_writable(P.prepared_root(self.store))
        shutil.rmtree(self.tmp, ignore_errors=True)

    @property
    def runs(self):
        return self.db.parent.parent

    def body(self, **over):
        sem = {"target_system": {"name": "blis", "description": "BLIS block-size sweep. "
                                 "Metric: capacity = max sustained req/s."},
               "models": {"session": "model-x"},
               "objective": {"metric": "capacity", "direction": "increase"},
               "blis": {"knobs": ["--kv-cpu-blocks"],
                        "limitations": ["BLIS does not model scorer weights"]},
               "run_id": "nousfixture-run-0001"}
        sem.update(over.pop("semantics", {}))
        b = {"question": "Does block size 32 beat 16 on capacity?", "task_type": "verify",
             "objective": "maximize capacity", "baseline": "block size 16",
             "hypothesis": "32 raises capacity by > 2 noise sd",
             "locks": {"blis_binary": "bin/blis-test", "blis_binary_sha256": self.bsha,
                       "catalog_commit": self.cat_commit, "model": "llama-3-8b"},
             "limits": {"max_iterations": 2}, "semantics": sem}
        b.update(over)
        return b

    def msg(self, *, mid=None, work_id=None, revision=1, **over):
        self.n += 1
        m = request(mid or f"req-{self.n}", work_id=work_id or f"w-{self.n}",
                    revision=revision, **self.body(**over))
        m["target"]["commit"] = self.commit
        return m

    def ctx(self, work_id="w-1", prep="prep-abcdef012345"):
        return AdapterContext(target=self.repo.resolve(), runs_dir=self.runs,
                              prep_dir=self.db.parent / "prepared" / prep, env=self.env,
                              repo_name="demo-repo", work_id=work_id)

    def submit(self, m):
        r = self.store.submit(m, env=self.env)
        self.assertTrue(r.ok, r.reasons)
        return m

    def prepare(self, work_id):
        return P.prepare(self.store, work_id, target=self.repo, env=self.env)

    def validate(self, m):
        return self.adapter.check_semantics(m) + self.adapter.validate(m, self.ctx())


# --- validate ---------------------------------------------------------------

class Validate(Base):
    def test_valid_request_passes(self):
        self.assertEqual(self.validate(self.msg()), [])

    def assertProblem(self, m, text):
        probs = self.validate(m)
        self.assertTrue(any(text in p for p in probs), probs)

    def test_required_semantics(self):
        for over, text in (
                ({"semantics": {"models": {}}}, "models.session"),
                ({"semantics": {"models": None}}, "models.session"),
                ({"semantics": {"target_system": {"name": "x"}}}, "target_system"),
                ({"semantics": {"target_system": "  "}}, "target_system"),
                ({"semantics": {"objective": {"metric": "m"}}}, "semantics.objective"),
                ({"semantics": {"objective": {"metric": "m", "direction": "up"}}},
                 "semantics.objective"),
                ({"limits": {}}, "max_iterations"),
                ({"semantics": {"blis": None}}, "blis.knobs"),
                ({"semantics": {"run_id": "r1"}}, "semantics.run_id"),
                ({"semantics": {"effort": "huge"}}, "semantics.effort"),
                ({"semantics": {"experiment_commands": ["("]}}, "bad regex"),
                ({"semantics": {"task_limits": {"max_cost": 3}}}, "task_limits")):
            with self.subTest(over=over):
                self.assertProblem(self.msg(**over), text)

    def test_availability_and_pin(self):
        self.hooks.probe_result.update(available=False, reason="nous not found")
        self.assertProblem(self.msg(), "nous-blis unavailable: nous not found")
        self.hooks.probe_result.update(available=True, engine_sha="f" * 40)
        self.assertProblem(self.msg(), "differs from the pin")
        self.hooks.probe_result.update(engine_sha=None)
        self.assertProblem(self.msg(), "cannot read the installed nous sha")
        self.hooks.probe_result.update(engine_sha=ENGINE_SHA, nous_on_path=False)
        self.assertProblem(self.msg(), "not on PATH")
        self.hooks.probe = lambda ctx: (_ for _ in ()).throw(RuntimeError("boom"))
        self.adapter.probe = self.hooks.probe
        self.assertProblem(self.msg(), "availability check failed: boom")

    def test_apparatus_locks(self):
        b = self.body()
        locks = b["locks"]
        for drop in N.REQUIRED_LOCKS:
            with self.subTest(drop=drop):
                lk = {k: v for k, v in locks.items() if k != drop}
                self.assertProblem(self.msg(locks=lk), f"locks.{drop}: required")
        self.assertProblem(self.msg(locks={**locks, "blis_binary_sha256": "0" * 64}),
                           "sha256 is")
        self.assertProblem(self.msg(locks={**locks, "catalog_commit": "0" * 40}),
                           "the lock says")
        self.assertProblem(self.msg(locks={**locks, "blis_binary": "bin/nope"}),
                           "does not exist")
        # sha256: prefix accepted; absolute path accepted
        self.assertEqual(self.validate(self.msg(locks={
            **locks, "blis_binary": str(self.binary),
            "blis_binary_sha256": "sha256:" + self.bsha})), [])
        self.hooks.probe_result["apparatus_root"] = None
        self.assertProblem(self.msg(), "no BLIS apparatus root")

    def test_non_blis_questions_rejected_with_reason(self):
        for over, label in (
                ({"question": "Does prefix-cache scorer weight 2 beat weight 1?"},
                 "EPP scorer weights"),
                ({"hypothesis": "WVA autoscaling reacts within 30 s"}, "WVA autoscaling"),
                ({"objective": "tune maxPrefixTokensToMatch"}, "approximate-prefix"),
                ({"question": "Does wide-EP help decode?"}, "wide-EP"),
                ({"question": "Does the batch gateway shed load?"}, "batch gateway")):
            with self.subTest(label=label):
                probs = self.validate(self.msg(**over))
                self.assertTrue(any("not BLIS-representable" in p and label in p
                                    for p in probs), probs)
        probs = self.validate(self.msg(semantics={"blis": {"knobs": ["--scorer-weight"]}}))
        self.assertTrue(any("knob '--scorer-weight'" in p for p in probs), probs)
        for k in ("--kv-cpu-blocks", "--kv-offload-threshold", "--kv-offload-config"):
            self.assertEqual(self.validate(self.msg(semantics={"blis": {"knobs": [k]}})), [])
        # Not flags of any locked BLIS binary (removed from blis.json 2026-11-10).
        for k in ("--pd-prefix-threshold", "--pd-decider", "--block-size-in-tokens",
                  "--hash-chain-block-tokens"):
            with self.subTest(removed=k):
                probs = self.validate(self.msg(semantics={"blis": {"knobs": [k]}}))
                self.assertTrue(any(f"knob {k!r}" in p for p in probs), probs)

    def test_speculative_indexing_knobs(self):
        for k in ("--kv-event-delay",
                  "epp:precise-prefix-cache-producer.speculativeIndexing",
                  "epp:precise-prefix-cache-producer.speculativeTTL"):
            with self.subTest(knob=k):
                self.assertEqual(self.validate(self.msg(semantics={"blis": {"knobs": [k]}})), [])
        self.assertEqual(self.validate(self.msg(
            question="Does a longer speculative-entry TTL raise prefix hits?")), [])
        for k in ("epp:precise-prefix-cache-producer.indexerConfig",
                  "epp:prefix-cache-scorer.weight",
                  "epp:precise-prefix-cache-producer.speculativeTTLx",
                  "precise-prefix-cache-producer.speculativeTTL"):
            with self.subTest(knob=k):
                probs = self.validate(self.msg(semantics={"blis": {"knobs": [k]}}))
                self.assertTrue(any(f"knob {k!r}" in p for p in probs), probs)

    def test_collisions(self):
        rid = "nousfixture-run-0001"
        (self.runs / rid).mkdir(parents=True)
        self.assertProblem(self.msg(), "work dir")
        shutil.rmtree(self.runs / rid)
        (self.repo / ".nous-experiments" / rid).mkdir(parents=True)
        self.assertProblem(self.msg(), "worktree")
        shutil.rmtree(self.repo / ".nous-experiments")
        git(self.repo, "branch", f"nous/{rid}")
        self.assertProblem(self.msg(), f"branch nous/{rid} already exists")
        git(self.repo, "branch", "-D", f"nous/{rid}")
        self.assertEqual(self.validate(self.msg()), [])
        self.hooks.processes = [(999999, "/x/bin/python /x/bin/nous run c.yaml --run-id z")]
        self.assertProblem(self.msg(), "already running")
        self.hooks.processes = [(999998, f"{self.binary} run --x")]
        self.assertProblem(self.msg(), "already running")
        self.hooks.processes = [(999997, "vim nous-notes.md"), (999996, "nous status x")]
        self.assertEqual(self.validate(self.msg()), [])
        self.adapter.list_processes = lambda: (_ for _ in ()).throw(RuntimeError("no ps"))
        self.assertProblem(self.msg(), "cannot check for running processes")

    def test_unknown_semantics_keys_rejected(self):
        for k in ("prompts", "metadata", "repo_path", "human_gates"):
            with self.subTest(k=k):
                self.assertProblem(self.msg(semantics={k: {"x": 1}}),
                                   f"semantics.{k}: not in nous adapter")
        self.assertProblem(self.msg(semantics={"target_system": {
            "description": "d", "repo_path": "/elsewhere"}}), "not rendered")

    def test_optional_semantics_type_and_value_problems(self):
        """Every optional-semantics rejection message, one bad value each.

        Each row names a branch semantics_problems() guards: the request is
        refused with that exact reason, not with a generic one (and never
        crashes on the wrong type)."""
        knobs = {"knobs": ["--kv-cpu-blocks"]}
        for over, text in (
                ({"semantics": {"models": {"session": "model-x", "extra": 1}}},
                 "semantics.models: only `session` is supported"),
                ({"question": "   "}, "question: required"),
                ({"objective": ""}, "objective: required"),
                ({"semantics": {"experiment_commands": "x"}},
                 "semantics.experiment_commands: non-empty list of regexes"),
                ({"semantics": {"experiment_commands": []}},
                 "semantics.experiment_commands: non-empty list of regexes"),
                ({"semantics": {"task_limits": {"max_runs": True}}},
                 "semantics.task_limits: only {max_runs: int}"),
                ({"semantics": {"task_limits": {"max_runs": "3"}}},
                 "semantics.task_limits: only {max_runs: int}"),
                ({"semantics": {"task_limits": {"max_cost": 3}}},
                 "semantics.task_limits: only {max_runs: int}"),
                ({"semantics": {"locked_workload": ["x"]}},
                 "semantics.locked_workload: must be a flat mapping"),
                ({"semantics": {"min_improvement": "high"}},
                 "semantics.min_improvement: must be a number"),
                ({"semantics": {"min_improvement": True}},
                 "semantics.min_improvement: must be a number"),
                ({"semantics": {"stall_iterations": 0}},
                 "semantics.stall_iterations: integer >= 1"),
                ({"semantics": {"stall_iterations": "2"}},
                 "semantics.stall_iterations: integer >= 1"),
                ({"semantics": {"session_scope": "room"}},
                 "semantics.session_scope: one of"),
                ({"semantics": {"secrets": ["1bad"]}},
                 "semantics.secrets: list of environment variable names"),
                ({"semantics": {"secrets": "FAKE_TOKEN"}},
                 "semantics.secrets: list of environment variable names"),
                ({"semantics": {"blis": "x"}}, "semantics.blis: must be a mapping"),
                ({"semantics": {"blis": {**knobs, "zzz": 1}}},
                 "semantics.blis: unknown keys ['zzz']"),
                ({"semantics": {"blis": {**knobs, "limitations": "x"}}},
                 "semantics.blis.limitations: list of text"),
                ({"semantics": {"blis": {**knobs, "limitations": [3]}}},
                 "semantics.blis.limitations: list of text")):
            with self.subTest(over=over):
                self.assertProblem(self.msg(**over), text)

    def test_locks_shape_problems(self):
        """Malformed lock values are refused for their shape, before any
        sha/commit comparison runs."""
        locks = self.body()["locks"]
        self.assertProblem(self.msg(locks={**locks, "blis_binary_sha256": "zz"}),
                           "locks.blis_binary_sha256: must be 64 hex digits")
        self.assertProblem(self.msg(locks={**locks, "catalog_commit": "zz"}),
                           "locks.catalog_commit: must be a full 40-hex commit")
        # A catalog dir that exists but is not a git checkout: the commit is
        # lock-shaped, so the failure is reported against the catalog itself.
        (self.app / "plain").mkdir()
        self.assertProblem(self.msg(semantics={"blis": {"knobs": ["--kv-cpu-blocks"],
                                                        "catalog": "plain"}}),
                           "is not a git checkout")

    def test_collision_flags_a_legacy_campaign_and_skips_own_pid(self):
        rid = "nousfixture-run-0001"
        legacy = self.repo / ".nous" / rid
        legacy.mkdir(parents=True)
        (legacy / "state.json").write_text("{}")
        self.assertProblem(self.msg(), f"a Nous campaign already exists at {legacy}")
        shutil.rmtree(self.repo / ".nous")
        # The process scan skips this process's own pid: a Nous command line
        # belonging to the caller is not a competing campaign.
        self.hooks.processes = [(os.getpid(), "/x/bin/nous run c.yaml --run-id z")]
        self.assertEqual(self.validate(self.msg()), [])
        # ... while any other pid running nous run/resume still is one.
        self.hooks.processes = [(999999, "/x/bin/nous run c.yaml --run-id z")]
        self.assertProblem(self.msg(), "already running")


# --- prepare / rendered YAML ------------------------------------------------

class Render(Base):
    def render(self, m, run_id="nousfixture-run-0001"):
        return self.adapter.dump_yaml(self.adapter.render_campaign(m, self.ctx(), run_id))

    def test_rendered_yaml_has_only_known_keys(self):
        full = {"experiment_commands": [r"\bblis\s+run\b"], "task_limits": {"max_runs": 40},
                "locked_workload": {"pods": 8}, "min_improvement": 0.05,
                "stall_iterations": 3, "effort": "high", "session_scope": "iteration",
                "secrets": ["HF_TOKEN"]}
        for sem in ({}, full):
            doc = yaml.safe_load(self.render(self.msg(semantics=sem)))
            self.assertLessEqual(set(doc), set(N.CAMPAIGN_KEYS))
            for k in N.IGNORED_KEYS:
                self.assertNotIn(k, doc)
            self.assertNotIn("prompts", json.dumps(doc))
            # what the pinned config parser requires (config.py)
            self.assertIn("research_question", doc)
            self.assertEqual(doc["target_system"]["repo_path"], str(self.repo.resolve()))
            self.assertEqual(set(doc["target_system"]), {"name", "repo_path", "description"})
            self.assertEqual(doc["models"], {"session": "model-x"})
            self.assertEqual(doc["locked_parameters"]["blis_binary_sha256"], self.bsha)
            self.assertIn("Hypothesis under test: 32 raises", doc["target_system"]["description"])
        self.assertEqual(doc["effort"], "high")

    def test_run_id_only_changes_the_run_id_line(self):
        m = self.msg()
        a = self.render(m, "nousfixture-aaaa-0001").decode().splitlines()
        b = self.render(m, "nousfixture-bbbb-0002").decode().splitlines()
        diff = [(x, y) for x, y in zip(a, b) if x != y]
        self.assertEqual(len(a), len(b))
        self.assertEqual(diff, [("run_id: nousfixture-aaaa-0001", "run_id: nousfixture-bbbb-0002")])

    def test_prepare_shape(self):
        m = self.submit(self.msg())
        res = self.prepare(m["work_id"])
        rec = res.record
        camp = P.prep_dir(self.store, res.prep_id) / "campaign.yaml"
        self.assertEqual(rec["argv"], ["nous", "run", str(camp), "--run-id",
                                       "nousfixture-run-0001"])
        self.assertEqual(rec["env_set"], {"NOUS_CAMPAIGN_PARENT": str(self.runs)})
        self.assertEqual(rec["native_run_id"], "nousfixture-run-0001")
        self.assertFalse(camp.stat().st_mode & 0o222, "campaign.yaml must be read-only")
        doc = yaml.safe_load(camp.read_text())
        self.assertEqual(doc["run_id"], "nousfixture-run-0001")
        mat = rec["material"]
        self.assertEqual(mat["invocation"]["argv"][-1], "{run_id}")
        self.assertEqual(mat["invocation"]["env_set"]["NOUS_CAMPAIGN_PARENT"], str(self.runs))
        self.assertEqual(mat["adapter_material"]["apparatus"]["binary_sha256"], self.bsha)
        self.assertEqual(mat["versions"], {"engine_sha": ENGINE_SHA, "pin": PIN})
        by = {x["name"]: x["enforced_by"] for x in mat["limits"]}
        self.assertEqual(by["max_iterations"], "engine")
        self.assertIn("nous", res.card)

    def test_prepare_reports_unknown_free_space(self):
        """The disk-usage probe is advisory: an OSError must not fail the
        preparation, and the card must say the space is unknown."""
        m = self.submit(self.msg())
        with mock.patch.object(N.shutil, "disk_usage", side_effect=OSError("no stat")):
            res = self.prepare(m["work_id"])
        self.assertIn("free space under the runs dir: unknown", res.card)
        # Control: with the probe working the card reports a real figure.
        res2 = self.prepare(self.submit(self.msg())["work_id"])
        self.assertRegex(res2.card, r"GB free under the runs dir")
        self.assertNotIn("unknown", [s for s in res2.card.splitlines()
                                     if "free under the runs dir" in s][0])

    def test_execute_builds_the_launch_spec_from_the_record(self):
        """execute() re-reads exactly what prepare() reserved: the supervisor
        spawns this spec for a real launch, with no adapter state in between."""
        m = self.submit(self.msg())
        rec = self.prepare(m["work_id"]).record
        spec = self.adapter.execute(rec, self.ctx())
        self.assertEqual(spec.argv, rec["argv"])
        self.assertEqual(spec.cwd, rec["cwd"])
        self.assertEqual(spec.env_set, rec["env_set"])
        self.assertEqual(spec.native_run_id, rec["native_run_id"])

    def test_evidence_survives_a_record_without_a_work_dir(self):
        """evidence() reads env_set/native_run_id to find the work dir; a
        record without them (or with a broken shape) still returns the base
        expected-artifacts evidence instead of crashing."""
        wd = self.runs / "nousfixture-run-0001"
        wd.mkdir(parents=True)
        (wd / "state.json").write_text("{}")
        record = {"env_set": {"NOUS_CAMPAIGN_PARENT": str(self.runs)},
                  "native_run_id": "nousfixture-run-0001", "expected_artifacts": []}
        ev = self.adapter.evidence({}, record, self.ctx())
        self.assertTrue(any(a["location"].endswith("state.json") for a in ev), ev)
        # KeyError (missing env_set) and TypeError (env_set not a mapping).
        self.assertEqual(self.adapter.evidence({}, {"expected_artifacts": []}, self.ctx()),
                         [])
        self.assertEqual(self.adapter.evidence({}, {"env_set": None,
                                                    "native_run_id": "x",
                                                    "expected_artifacts": []},
                                               self.ctx()), [])

    def test_generated_run_id_is_long_unique_and_non_material(self):
        a = self.submit(self.msg(semantics={"run_id": None}))
        b = self.submit(self.msg(semantics={"run_id": None}))
        ra, rb = self.prepare(a["work_id"]), self.prepare(b["work_id"])
        self.assertNotEqual(ra.record["native_run_id"], rb.record["native_run_id"])
        self.assertGreater(len(ra.record["native_run_id"]), 16)
        self.assertTrue(ra.record["native_run_id"].endswith(ra.prep_id))
        # different work ids and run ids, same experiment -> same digest
        self.assertEqual(ra.material_digest, rb.material_digest)

    def test_run_id_is_not_material(self):
        a = self.submit(self.msg(semantics={"run_id": "nousfixture-xxxx-0001"}))
        b = self.submit(self.msg(semantics={"run_id": "nousfixture-yyyy-0002"}))
        self.assertEqual(self.prepare(a["work_id"]).material_digest,
                         self.prepare(b["work_id"]).material_digest)


# --- material mutation after approval ---------------------------------------

MUTATIONS = {
    "target_system": {"name": "blis", "description": "a different description"},
    "models": {"session": "model-y"},
    "objective": {"metric": "capacity", "direction": "decrease"},
    "blis": {"knobs": ["--kv-offload-threshold"]},
    "experiment_commands": [r"\bblis\s+run\b"],
    "task_limits": {"max_runs": 7},
    "locked_workload": {"pods": 4},
    "min_improvement": 0.1,
    "stall_iterations": 4,
    "effort": "low",
    "session_scope": "iteration",
    "secrets": ["HF_TOKEN"],
}


class MaterialMutation(Base):
    def setUp(self):
        super().setUp()
        self.env["HF_TOKEN"] = "hf-planted-secret-0a1b2c3d4e5f"
        m = self.submit(self.msg(work_id="w-mat"))
        self.res = self.prepare("w-mat")
        AP.approve(self.store, self.res.prep_id, code=self.res.short_code, source="chat",
                   quote=go(self.res.short_code), env=self.env)
        self.assertTrue(P.revalidate(self.store, self.res.prep_id, env=self.env).ok)
        self.base = m

    def test_every_material_field_is_covered(self):
        self.assertEqual(set(MUTATIONS), set(N.NousAdapter.MATERIAL_FIELDS))

    def test_each_material_field_mutation_is_rejected_after_approval(self):
        rev = 1
        seen = {self.res.material_digest}
        for field, value in MUTATIONS.items():
            with self.subTest(field=field):
                rev += 1
                m = copy.deepcopy(self.base)
                m["message_id"] = f"req-mut-{field}"
                m["revision"] = rev
                m["body"]["semantics"][field] = value
                self.submit(m)
                rv = P.revalidate(self.store, self.res.prep_id, env=self.env)
                self.assertFalse(rv.ok)
                self.assertTrue(any("newer request revision" in p for p in rv.problems))
                new = self.prepare("w-mat")
                self.assertNotIn(new.material_digest, seen, field)
                seen.add(new.material_digest)
                with self.assertRaises(AP.ApprovalError):
                    AP.approve(self.store, new.prep_id, code=self.res.short_code,
                               source="chat", quote=go(self.res.short_code), env=self.env)

    def test_apparatus_change_after_approval_blocks_launch(self):
        P.make_writable(self.binary.parent)
        self.binary.write_bytes(BINARY + b"# patched\n")
        rv = P.revalidate(self.store, self.res.prep_id, env=self.env)
        self.assertFalse(rv.ok)
        self.assertTrue(any("sha256 is" in p for p in rv.problems), rv.problems)

    def test_engine_upgrade_after_approval_blocks_launch(self):
        self.hooks.probe_result["engine_sha"] = "e224cc1" + "0" * 33
        rv = P.revalidate(self.store, self.res.prep_id, env=self.env)
        self.assertTrue(any("version drift: engine_sha" in p for p in rv.problems),
                        rv.problems)

    def test_collision_after_approval_blocks_launch(self):
        (self.runs / "nousfixture-run-0001").mkdir(parents=True)
        rv = P.revalidate(self.store, self.res.prep_id, env=self.env)
        self.assertTrue(any("work dir" in p for p in rv.problems), rv.problems)


# --- classify: plan §1 Q2, row by row ---------------------------------------

class Classify(Base):
    RID = "nousclassify-run-0001"

    def record(self):
        return {"env_set": {"NOUS_CAMPAIGN_PARENT": str(self.runs)},
                "native_run_id": self.RID,
                "material": {"request": {"semantics": {"blis": {
                    "limitations": ["BLIS does not model scorer weights"]}}}}}

    def place(self, fixture, mutate=None):
        wd = self.runs / self.RID
        if wd.exists():
            shutil.rmtree(wd)
        if fixture is not None:
            shutil.copytree(FIX / fixture, wd)
        else:
            wd.mkdir(parents=True)
        if mutate:
            mutate(wd)
        return wd

    def classify(self, fixture, *, cancel=False, rc=0, mutate=None):
        self.place(fixture, mutate)
        inv = {"inv_id": "inv-1", "exit_code": rc,
               "cancel_requested_at": "2026-11-09T00:00:00.000Z" if cancel else None}
        return self.adapter.classify(inv, self.record(), self.ctx())

    def test_halt_in_ledger(self):
        ns = self.classify("workdir-halt")
        self.assertEqual((ns.state, ns.native_stop_reason, ns.partial),
                         ("failed", "halt", True))

    def test_halt_json_only_design_halt(self):
        led = json.loads((FIX / "workdir-halt-design/ledger.json").read_text())
        self.assertNotEqual(led[-1]["stop_reason"], "halt")   # the R0 correction
        ns = self.classify("workdir-halt-design")
        self.assertEqual((ns.state, ns.native_stop_reason, ns.partial),
                         ("failed", "halt", True))
        self.assertIn("DESIGN", ns.reason)

    def test_halt_wins_over_a_completed_stop(self):
        def add_halt(wd):
            (wd / "halt.json").write_text('{"phase": "EXECUTE", "message": "m"}')
        ns = self.classify("workdir-example", mutate=add_halt)
        self.assertEqual(ns.state, "failed")

    def test_completed_rows(self):
        self.assertEqual(self.classify("workdir-example").as_dict()["state"], "completed")
        ns = self.classify("workdir-max-iterations")
        self.assertEqual((ns.state, ns.native_stop_reason), ("completed", "max_iterations"))

        def stall(wd):
            led = json.loads((wd / "ledger.json").read_text())
            led[-1]["stop_reason"] = "stall"
            (wd / "ledger.json").write_text(json.dumps(led))
        ns = self.classify("workdir-max-iterations", mutate=stall)
        self.assertEqual((ns.state, ns.native_stop_reason), ("completed", "stall"))

    def test_completed_needs_final_check_not_exit_code(self):
        def drop_final(wd):
            (wd / "final_check.json").unlink()
        ns = self.classify("workdir-example", rc=0, mutate=drop_final)
        self.assertEqual(ns.state, "failed")
        self.assertIn("resumable", ns.reason)

    def test_stop_sentinel_with_cancel(self):
        ns = self.classify("workdir-stop-sentinel", cancel=True)
        self.assertEqual((ns.state, ns.native_stop_reason, ns.partial),
                         ("cancelled", "stop_sentinel", True))

    def test_stop_sentinel_without_cancel(self):
        ns = self.classify("workdir-stop-sentinel", cancel=False)
        self.assertEqual((ns.state, ns.native_stop_reason), ("completed", "stop_sentinel"))
        self.assertTrue(any("outside the runtime" in w for w in ns.warnings))

    def test_no_baseline(self):
        ns = self.classify("workdir-no-baseline")
        self.assertEqual((ns.state, ns.native_stop_reason), ("failed", "no_baseline"))

    def test_exited_without_final_check(self):
        for rc in (1, 130, None):
            with self.subTest(rc=rc):
                ns = self.classify("workdir-running", rc=rc)
                self.assertEqual(ns.state, "failed")
                self.assertIn("resumable", ns.reason)

    def test_sigint_after_cancel_is_cancelled(self):
        ns = self.classify("workdir-running", rc=130, cancel=True)
        self.assertEqual((ns.state, ns.partial), ("cancelled", True))

    def test_unclassified_final_check(self):
        def no_no_baseline(wd):
            st = json.loads((wd / "state.json").read_text())
            st["stop_reason"] = None
            (wd / "state.json").write_text(json.dumps(st))
        ns = self.classify("workdir-no-baseline", mutate=no_no_baseline)
        self.assertEqual(ns.state, "failed")
        self.assertIn("unclassified_native_state", ns.reason)

    def test_no_state_json(self):
        ns = self.classify(None, rc=1)
        self.assertEqual(ns.state, "failed")
        self.assertIn("no Nous state.json", ns.reason)

    def test_evidence_lists_native_files(self):
        self.place("workdir-stop-sentinel")
        kinds = {e["kind"] for e in self.adapter.evidence({}, self.record(), self.ctx())}
        self.assertTrue({"nous-state", "nous-ledger", "nous-final-check",
                         "nous-stop-sentinel"} <= kinds, kinds)

    # --- collect ------------------------------------------------------------

    def collect(self, fixture, **kw):
        self.place(fixture)
        inv = {"inv_id": "inv-1", "exit_code": kw.get("rc", 0),
               "cancel_requested_at": None}
        return self.adapter.collect(inv, self.record(), self.ctx())

    def test_collect_classifications_and_limitations(self):
        c = self.collect("workdir-max-iterations")
        self.assertEqual(c["state"], "collected")
        self.assertFalse(c["partial"])
        got = {(h["iteration"], h["id"]): h for h in c["hypotheses"]}
        self.assertEqual(got[(1, "H1")]["classification"], "other")
        self.assertEqual(got[(1, "H1")]["partial_note"], "holds on W1-W3 only")
        self.assertEqual(got[(1, "H2")]["classification"], "not_supported")
        self.assertEqual(got[(2, "H1")]["classification"], "inconclusive")
        self.assertEqual(got[(1, "H1")]["statement"]["mechanism"], "bigger blocks raise hits")
        self.assertTrue(got[(1, "H1")]["statement_ref"].endswith(".bundle.locked.json"))
        srcs = {x["source"] for x in c["limitations"]}
        for s in ("nous:findings.reason", "nous:findings.noise_unmeasured",
                  "nous:final_check.unmeasured_noise_findings", "nous:ledger.status",
                  "coordinator"):
            self.assertIn(s, srcs)
        self.assertEqual(c["outcome"]["classification"], "unavailable")
        self.assertTrue(c["outcome"]["reason"])
        self.assertEqual(c["spend"], {**c["spend"], "reported_usd": 0.85,
                                      "completeness": "partial"})
        self.assertEqual(c["claims"][0]["id"], "C1")
        self.assertTrue(c["methods"]["plans"])
        json.dumps(c)   # serializable

    def test_collect_halt_is_partial(self):
        c = self.collect("workdir-halt-design")
        self.assertTrue(c["partial"])
        self.assertTrue(all(h["partial"] for h in c["hypotheses"]))
        srcs = [x["source"] for x in c["limitations"]]
        self.assertIn("nous:halt.json", srcs)
        self.assertIn("runtime", srcs)
        self.assertEqual(c["native_state"]["state"], "failed")

    def test_collect_example_and_answer_invalid(self):
        c = self.collect("workdir-example")
        self.assertEqual([h["classification"] for h in c["hypotheses"]],
                         ["supported", "not_supported"])
        self.assertTrue(c["usable"])
        c = self.collect("workdir-answer-invalid")
        self.assertIn("nous:final_check.answer_valid",
                      {x["source"] for x in c["limitations"]})

    def test_collect_unavailable_when_not_a_work_dir(self):
        c = self.collect(None)
        self.assertEqual(c["state"], "unavailable")
        self.assertIn("state.json", c["reason"])

    def test_collect_is_read_only(self):
        wd = self.place("workdir-max-iterations")
        before = {p: p.stat().st_mtime_ns for p in wd.rglob("*")}
        inv = {"inv_id": "inv-1", "exit_code": 0, "cancel_requested_at": None}
        self.adapter.collect(inv, self.record(), self.ctx())
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in wd.rglob("*")})

    # --- cancel -------------------------------------------------------------

    def test_cancel_uses_nous_stop(self):
        wd = self.place("workdir-running")
        r = self.adapter.cancel({"inv_id": "inv-9"}, self.record(), self.ctx())
        self.assertTrue(r["requested"])
        self.assertEqual(r["mechanism"], "nous-stop")
        argv, env = self.hooks.cmds[-1]
        self.assertEqual(argv[:3], ["nous", "stop", str(wd)])
        self.assertIn("inv-9", argv[-1])
        self.assertEqual(env["NOUS_CAMPAIGN_PARENT"], str(self.runs))
        self.hooks.rc = 1
        r = self.adapter.cancel({"inv_id": "inv-9"}, self.record(), self.ctx())
        self.assertFalse(r["requested"])
        self.assertIn("exited 1", r["error"])

    def test_cancel_before_work_dir_exists(self):
        r = self.adapter.cancel({"inv_id": "inv-9"}, self.record(), self.ctx())
        self.assertFalse(r["requested"])
        self.assertEqual(self.hooks.cmds, [])

    def test_cancel_interrupt_is_sigint_never_sigterm(self):
        with mock.patch.object(N.NousAdapter, "interrupt_engine",
                               return_value={"mechanism": "sigint", "requested": True,
                                             "error": None}) as ie:
            r = self.adapter.cancel({"inv_id": "inv-9"}, self.record(), self.ctx(),
                                    interrupt=True)
        ie.assert_called_once()
        self.assertEqual(r["mechanism"], "sigint")
        src = Path(N.__file__).read_text()
        self.assertNotIn("SIGTERM)", src)
        self.assertNotIn("stop_process_tree(", src)


class Registry(unittest.TestCase):
    def test_shipped_nous_adapter_registered(self):
        self.assertIsInstance(A.get("nous"), N.NousAdapter)

    def test_default_probe_without_apparatus(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = make_repo(tmp)
        with mock.patch.dict("os.environ", {"INVESTIGATION_CONFIG_HOME": str(tmp / "cfg")}):
            ctx = AdapterContext(target=repo, runs_dir=tmp / "runs", prep_dir=tmp / "p",
                                 env={"PATH": "/usr/bin:/bin"})
            p = N.default_probe(ctx)
        self.assertFalse(p["available"])
        self.assertIn("BLIS apparatus", p["reason"])
        self.assertEqual(p["pin"], PIN)
        self.assertEqual(p["pin_sha"], "e224cc1")

    def test_default_probe_reports_unparseable_configs(self):
        """A ConfigError from either config layer is a reason, not a crash."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        repo = make_repo(tmp)
        cfg = tmp / "cfg"
        cfg.mkdir()
        (cfg / "config.yaml").write_text("engines: [broken\n")
        ctx = AdapterContext(target=repo, runs_dir=tmp / "runs", prep_dir=tmp / "p",
                             env={"PATH": "/usr/bin:/bin"})
        with mock.patch.dict("os.environ", {"INVESTIGATION_CONFIG_HOME": str(cfg)}):
            p = N.default_probe(ctx)
        self.assertFalse(p["available"])
        self.assertIn("machine config:", p["reason"])
        # A broken committed repo config, with the machine layer healthy:
        # same treatment, different layer.
        (cfg / "config.yaml").write_text("engines: {}\n")
        (repo / "investigations").mkdir()
        (repo / "investigations" / "config.yaml").write_text("clusters: [broken\n")
        with mock.patch.dict("os.environ", {"INVESTIGATION_CONFIG_HOME": str(cfg)}):
            p = N.default_probe(ctx)
        self.assertFalse(p["available"])
        self.assertIn("repo config:", p["reason"])


class DefaultHooks(unittest.TestCase):
    """The module-level hooks the shipped adapter uses when no test injects
    its own: they read the real /proc (or ps) and really run the command."""

    def test_default_list_processes_reads_proc_or_ps(self):
        out = N.default_list_processes()
        self.assertIsInstance(out, list)
        for pid, cmd in out:
            self.assertIsInstance(pid, int)
            self.assertIsInstance(cmd, str)
        # This process is on the list (its own /proc entry is readable).
        self.assertTrue(any(pid == os.getpid() for pid, _ in out))
        # The ps fallback parses pid/command pairs and skips junk lines.
        ps_text = "  123 /bin/sleep x\njunk\n45 cmd here\n"
        with mock.patch.object(procid, "_proc_usable", return_value=False), \
             mock.patch.object(procid, "_run_ps", return_value=ps_text):
            self.assertEqual(N.default_list_processes(),
                             [(123, "/bin/sleep x"), (45, "cmd here")])
        # No /proc and a failing ps: a loud error, never a silent [].
        with mock.patch.object(procid, "_proc_usable", return_value=False), \
             mock.patch.object(procid, "_run_ps", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "cannot list processes"):
                N.default_list_processes()

    def test_default_run_cmd_captures_output_and_reports_failures(self):
        rc, out = N.default_run_cmd([sys.executable, "-c", "print('probe-out')"], {})
        self.assertEqual(rc, 0)
        self.assertEqual(out, "probe-out")
        # A missing binary is (-1, message), not an exception.
        rc, out = N.default_run_cmd(["/definitely-not-a-binary-x"], {})
        self.assertEqual(rc, -1)
        self.assertIn("/definitely-not-a-binary-x", out)


class CollectModule(unittest.TestCase):
    """The functions extracted into collect.py (the CLI's bundle is unchanged)."""

    def test_normalize_matches_build_bundle(self):
        wd = FIX / "workdir-example"
        n = C.normalize(wd)
        b = C.build_bundle(wd)
        n["bundle"].pop("collected_at")
        b.pop("collected_at")
        self.assertEqual(n["bundle"], b)
        self.assertEqual(n["methods"]["baseline"]["git_sha"], "3f2a9c1")
        self.assertTrue(n["methods"]["experiment_runs"])

    def test_status_mapping(self):
        self.assertEqual(C.STATUS_CLASSIFICATION, {
            "CONFIRMED": "supported", "REFUTED": "not_supported",
            "INCONCLUSIVE": "inconclusive", "PARTIALLY_CONFIRMED": "other"})

    def test_cli_output_has_no_normalized_keys(self):
        r = subprocess.run([sys.executable, str(Path(C.__file__)), str(FIX / "workdir-example")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("hypotheses", json.loads(r.stdout))


if __name__ == "__main__":
    unittest.main()
