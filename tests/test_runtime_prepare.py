"""R2: preparation, approval, pre-launch revalidation (plan §5 R2).

Uses the test-only fake adapter (tests/_runtime_fake_adapter.py) and
throwaway git repos in temp dirs.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _runtime_fake_adapter as fake  # noqa: E402
from _runtime_util import envelope, request  # noqa: E402
from ains_runtime import adapters as A  # noqa: E402
from ains_runtime import approval as AP  # noqa: E402
from ains_runtime import envpolicy, gitstate  # noqa: E402
from ains_runtime import preparation as P  # noqa: E402
from ains_runtime import store as S  # noqa: E402
from ains_runtime.canonical import short_code  # noqa: E402

HERE = Path(__file__).resolve().parent
FAKE_CLI = HERE / "_runtime_fake_cli.py"
SECRET = "sk-planted-0c9d8e7f6a5b4c3d"


def git(repo, *args):
    return subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
                           "-c", "commit.gpgsign=false", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def make_repo(root: Path, name="demo-repo") -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "README").write_text("hello\n")
    git(repo, "add", "README")
    git(repo, "commit", "-qm", "init")
    return repo


def go(code):
    return f"yes, go ahead with {code}"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = make_repo(self.tmp)
        self.commit = git(self.repo, "rev-parse", "HEAD")
        self.db = self.tmp / "runs" / "demo-repo" / "s1" / ".runtime" / S.DB_NAME
        self.store = S.open_store(self.db)
        self.adapter, self._restore = fake.use()
        self.env = {"HOME": "/home/t", "ANTHROPIC_BASE_URL": "https://a.example",
                    "CLAUDE_CODE_ENTRYPOINT": "cli"}

    def tearDown(self):
        self._restore()
        self.store.close()
        root = P.prepared_root(self.store)
        P.make_writable(root)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def req(self, mid="req-1", revision=1, **over):
        m = request(mid, revision=revision, **over)
        m["target"]["commit"] = self.commit
        return m

    def submit(self, msg=None):
        res = self.store.submit(msg or self.req(), env=self.env)
        self.assertTrue(res.ok, res.reasons)

    def prepare(self, work_id="w-1", **kw):
        return P.prepare(self.store, work_id, target=self.repo, env=self.env, **kw)

    def approve(self, res, quote=None):
        return AP.approve(self.store, res.prep_id, code=res.short_code, source="chat",
                          quote=quote or go(res.short_code), env=self.env)

    def reval(self, prep_id, env=None):
        return P.revalidate(self.store, prep_id, env=self.env if env is None else env)


class Prepare(Base):
    def test_prepare_records_and_writes_read_only_inputs(self):
        self.submit()
        res = self.prepare()
        pdir = P.prep_dir(self.store, res.prep_id)
        self.assertEqual(sorted(p.name for p in pdir.iterdir()), ["campaign.yaml", "card.md"])
        for f in pdir.iterdir():
            self.assertFalse(os.stat(f).st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        self.assertFalse(os.stat(pdir).st_mode & stat.S_IWUSR)
        stored = self.store.get_preparation(res.prep_id)
        self.assertEqual(stored["material_digest"], res.material_digest)
        mat = stored["record"]["material"]
        self.assertEqual(mat["target"]["commit"], self.commit)
        self.assertTrue(mat["target"]["clean"])
        self.assertEqual(mat["versions"], {"engine_sha": "e" * 40})
        # Placeholders, not paths, inside the material copy.
        self.assertIn("{prep_dir}/campaign.yaml", mat["invocation"]["argv"])
        self.assertIn("{run_id}", mat["invocation"]["argv"])
        self.assertEqual(res.short_code, short_code(res.material_digest))

    def test_unknown_work_and_unknown_capability_refused(self):
        with self.assertRaisesRegex(P.PrepareError, "no work"):
            self.prepare("w-nope")
        self.submit(self.req(capability="spotlights-telemetry"))
        with self.assertRaisesRegex(P.PrepareError, "no adapter"):
            self.prepare()

    def test_adapter_validation_and_material_policy_refusals(self):
        sem = {"models": {}, "run_id": "r1", "surprise": 1}
        self.submit(self.req(semantics=sem))
        with self.assertRaises(P.PrepareError) as cm:
            self.prepare()
        joined = " ".join(cm.exception.reasons)
        self.assertIn("models.session", joined)
        self.assertIn("semantics.surprise: not in", joined)
        self.assertFalse(P.prepared_root(self.store).exists())
        self.assertEqual(self.store.list_preparations(), [])

    def test_target_mismatch_refused(self):
        m = self.req()
        m["target"]["commit"] = "f" * 40
        self.submit(m)
        with self.assertRaisesRegex(P.PrepareError, "expected " + "f" * 40):
            self.prepare()
        m2 = self.req("req-2", work_id="w-2")
        m2["target"]["repo_name"] = "other"
        self.submit(m2)
        with self.assertRaisesRegex(P.PrepareError, "request is for 'other'"):
            self.prepare("w-2")

    def test_equivalent_requests_get_the_same_digest(self):
        # run_id is non-material, and ids / next_step are not material.
        self.submit()
        a = self.prepare()
        sem = {"models": {"session": "model-x"}, "run_id": "r-other"}
        self.submit(self.req("req-2", work_id="w-2", semantics=sem,
                             next_step={k: "x" for k in ("interpretation_of_prior",
                                                         "remaining_uncertainty",
                                                         "expected_information_gain",
                                                         "rationale")}))
        b = self.prepare("w-2")
        self.assertEqual(a.material_digest, b.material_digest)
        ya = (P.prep_dir(self.store, a.prep_id) / "campaign.yaml").read_text().splitlines()
        yb = (P.prep_dir(self.store, b.prep_id) / "campaign.yaml").read_text().splitlines()
        self.assertEqual([x for x in ya if not x.startswith("run_id")],
                         [x for x in yb if not x.startswith("run_id")])
        self.assertNotEqual(ya, yb)

    def test_each_material_field_changes_the_digest(self):
        self.submit()
        base = self.prepare().material_digest
        mutations = {
            "question": dict(question="Another question?"),
            "objective": dict(objective="maximize throughput"),
            "task_type": dict(task_type="explain"),
            "baseline": dict(baseline="weight 3"),
            "hypothesis": dict(hypothesis="other"),
            "locks": dict(locks={"seed": 1}),
            "limits": dict(limits={"max_iterations": 4}),
            "models": dict(semantics={"models": {"session": "model-y"}, "run_id": "r1"}),
            "experiment": dict(semantics={"models": {"session": "model-x"},
                                          "run_id": "r1", "experiment": "e2"}),
        }
        for i, (name, over) in enumerate(mutations.items(), start=2):
            with self.subTest(name):
                self.submit(self.req(f"req-{i}", work_id=f"w-{i}", **over))
                self.assertNotEqual(self.prepare(f"w-{i}").material_digest, base)
        with self.subTest("material env var"):
            self.submit(self.req("req-90", work_id="w-90"))
            self.env["FAKE_ENGINE_MODE"] = "fast"
            self.assertNotEqual(self.prepare("w-90").material_digest, base)
            del self.env["FAKE_ENGINE_MODE"]
        with self.subTest("engine sha"):
            self.submit(self.req("req-91", work_id="w-91"))
            self.adapter.engine_sha = "d" * 40
            self.assertNotEqual(self.prepare("w-91").material_digest, base)
            self.adapter.engine_sha = "e" * 40
        with self.subTest("observed env is NOT material (D9)"):
            self.submit(self.req("req-92", work_id="w-92"))
            self.env["ANTHROPIC_BASE_URL"] = "https://elsewhere.example"
            self.env["CLAUDE_EXTRA"] = "1"
            self.assertEqual(self.prepare("w-92").material_digest, base)


class CleanTree(Base):
    def test_d4_ignored_paths_do_not_block(self):
        for d in ("investigations/s1", ".nous-experiments/r1"):
            (self.repo / d).mkdir(parents=True)
            (self.repo / d / "f.txt").write_text("x")
        self.assertEqual(gitstate.dirty_paths(self.repo), [])
        # also from a subdirectory
        self.assertEqual(gitstate.dirty_paths(self.repo / "investigations"), [])

    def test_anything_else_blocks(self):
        cases = {
            "untracked": lambda: (self.repo / "new.txt").write_text("x"),
            "modified": lambda: (self.repo / "README").write_text("changed\n"),
            "staged": lambda: ((self.repo / "s.txt").write_text("x"),
                               git(self.repo, "add", "s.txt")),
            "lookalike": lambda: ((self.repo / "investigationsX").mkdir(),
                                  (self.repo / "investigationsX" / "f").write_text("x")),
        }
        for name, dirty in cases.items():
            with self.subTest(name):
                dirty()
                self.assertTrue(gitstate.dirty_paths(self.repo))
                git(self.repo, "reset", "-q", "--hard")
                git(self.repo, "clean", "-qfd")
                self.assertEqual(gitstate.dirty_paths(self.repo), [])

    def test_dirty_target_refused_at_prepare(self):
        self.submit()
        (self.repo / "scratch.py").write_text("x")
        with self.assertRaisesRegex(P.PrepareError, "uncommitted changes.*scratch.py"):
            self.prepare()

    def test_not_a_repo(self):
        with self.assertRaises(gitstate.GitStateError):
            gitstate.snapshot(self.tmp / "runs")


class Approve(Base):
    def setUp(self):
        super().setUp()
        self.submit()
        self.res = self.prepare()

    def test_missing_or_wrong_code_and_missing_quote_refused(self):
        code = self.res.short_code
        bad = [
            dict(code=None, quote=go(code)),
            dict(code="", quote=go(code)),
            dict(code="0" * 12 if code != "0" * 12 else "1" * 12, quote=go(code)),
            dict(code=code[:6], quote=go(code)),
            dict(code=code.upper(), quote=go(code)),
            dict(code=code, quote=None),
            dict(code=code, quote="   "),
            dict(code=code, quote="yes go"),  # quote lacks the code
        ]
        for i, kw in enumerate(bad):
            with self.subTest(i):
                with self.assertRaises(AP.ApprovalError):
                    AP.approve(self.store, self.res.prep_id, source="chat", env=self.env,
                               **kw)
        with self.assertRaisesRegex(AP.ApprovalError, "source"):
            AP.approve(self.store, self.res.prep_id, code=code, source="cli",
                       quote=go(code), env=self.env)
        with self.assertRaisesRegex(AP.ApprovalError, "source"):
            AP.approve(self.store, self.res.prep_id, code=code, source=None,
                       quote=go(code), env=self.env)
        self.assertIsNone(self.store.get_approval(self.res.prep_id))

    def test_unknown_preparation(self):
        with self.assertRaisesRegex(AP.ApprovalError, "no preparation"):
            AP.approve(self.store, "prep-nope", code="a" * 12, source="chat",
                       quote="a" * 12, env=self.env)

    def test_approval_records_source_and_quote(self):
        a = self.approve(self.res)
        row = self.store.get_approval(self.res.prep_id)
        self.assertEqual(row["approval_id"], a.approval_id)
        self.assertEqual(row["source"], "chat")
        self.assertEqual(row["owner_quote"], go(self.res.short_code))
        self.assertEqual(row["material_digest"], self.res.material_digest)
        self.assertIsNone(row["consumed_at"])

    def test_single_use_and_bound_to_one_digest(self):
        self.approve(self.res)
        with self.assertRaisesRegex(AP.ApprovalError, "already approved"):
            self.approve(self.res)
        # The code of one preparation does not approve another.
        self.submit(self.req("req-2", work_id="w-2", question="Other?"))
        other = self.prepare("w-2")
        with self.assertRaises(AP.ApprovalError):
            AP.approve(self.store, other.prep_id, code=self.res.short_code,
                       source="chat", quote=go(self.res.short_code), env=self.env)
        # Consumed once by the launch grant; never again.
        rv = self.reval(self.res.prep_id)
        self.assertTrue(rv.ok, rv.problems)
        self.store.reserve_invocation("inv-1", self.res.prep_id,
                                      expected_digest=rv.material_digest)
        self.assertIsNotNone(self.store.get_approval(self.res.prep_id)["consumed_at"])
        rv2 = self.reval(self.res.prep_id)
        self.assertFalse(rv2.ok)
        self.assertTrue(any("already used" in p for p in rv2.problems), rv2.problems)
        with self.assertRaisesRegex(S.GrantError, "already used|already exists|UNIQUE"):
            self.store.reserve_invocation("inv-2", self.res.prep_id)

    def test_superseded_revision_cannot_be_approved(self):
        self.submit(self.req("req-2", revision=2, question="Revised?"))
        with self.assertRaisesRegex(AP.ApprovalError, "newer request revision"):
            self.approve(self.res)

    def test_tampered_preparation_record_is_refused(self):
        # A stored record whose material no longer digests to the recorded
        # material_digest (a hand-edited or corrupted DB row) must not be
        # approvable: the code would "match" a record that was never prepared.
        stored = self.store.get_preparation(self.res.prep_id)
        rec = dict(stored["record"])
        rec["material"] = dict(rec["material"], question="tampered?")
        self.store.conn.execute("UPDATE preparations SET record_json = ?"
                                " WHERE prep_id = ?",
                                (json.dumps(rec), self.res.prep_id))
        self.store.conn.commit()
        with self.assertRaisesRegex(AP.ApprovalError, "does not match its digest"):
            self.approve(self.res)
        self.assertIsNone(self.store.get_approval(self.res.prep_id))

    def test_secret_in_quote_is_refused_without_echoing_it(self):
        # approve() pre-checks the code and the quote's shape, but the
        # secret-value scan happens in Store.insert_approval; its StoreError
        # must surface as an ApprovalError that names the variable without
        # echoing the secret value.
        self.env["FAKE_API_TOKEN"] = SECRET
        try:
            with self.assertRaisesRegex(AP.ApprovalError, "FAKE_API_TOKEN") as cm:
                AP.approve(self.store, self.res.prep_id, code=self.res.short_code,
                           source="chat", quote=go(self.res.short_code) + " " + SECRET,
                           env=self.env)
        finally:
            del self.env["FAKE_API_TOKEN"]
        self.assertNotIn(SECRET, str(cm.exception))
        self.assertIsNone(self.store.get_approval(self.res.prep_id))


class Revalidate(Base):
    """The pre-launch checks R3's launch calls (spec §7 step 6)."""

    def setUp(self):
        super().setUp()
        self.submit()
        self.res = self.prepare()

    def assert_blocked(self, pattern, env=None):
        rv = self.reval(self.res.prep_id, env)
        self.assertFalse(rv.ok)
        self.assertTrue(any(pattern in p for p in rv.problems), rv.problems)
        return rv

    def test_launch_without_approval_refused(self):
        self.assert_blocked("has no approval")
        with self.assertRaisesRegex(S.GrantError, "no approval"):
            self.store.reserve_invocation("inv-1", self.res.prep_id)

    def test_approved_and_unchanged_passes(self):
        self.approve(self.res)
        rv = self.reval(self.res.prep_id)
        self.assertTrue(rv.ok, rv.problems)
        self.assertEqual(rv.warnings, [])
        self.assertEqual(rv.material_digest, self.res.material_digest)

    def test_unknown_preparation(self):
        rv = self.reval("prep-nope")
        self.assertFalse(rv.ok)
        self.assertIn("grant nothing", rv.problems[0])

    def test_dirty_target_between_prepare_and_launch(self):
        self.approve(self.res)
        (self.repo / "late.txt").write_text("x")
        self.assert_blocked("uncommitted changes")

    def test_head_drift(self):
        self.approve(self.res)
        (self.repo / "f2").write_text("x")
        git(self.repo, "add", "f2")
        git(self.repo, "commit", "-qm", "drift")
        self.assert_blocked("target HEAD is")

    def test_engine_version_drift(self):
        self.approve(self.res)
        self.adapter.engine_sha = "d" * 40
        self.assert_blocked("version drift: engine_sha")

    def test_adapter_version_drift(self):
        self.approve(self.res)
        self.adapter.version = "fake-0.2"
        self.assert_blocked("adapter version changed")

    def test_runtime_version_drift(self):
        self.approve(self.res)
        orig = P.RUNTIME_VERSION
        P.RUNTIME_VERSION = "9.9.9"
        try:
            self.assert_blocked("runtime version changed")
        finally:
            P.RUNTIME_VERSION = orig

    def test_adapter_missing_at_launch(self):
        self.approve(self.res)
        A.unregister("nous")
        self.assert_blocked("no adapter")

    def test_prepared_input_tampered(self):
        self.approve(self.res)
        f = P.prep_dir(self.store, self.res.prep_id) / "campaign.yaml"
        P.make_writable(f.parent)
        f.write_text(f.read_text() + "extra: 1\n")
        self.assert_blocked("campaign.yaml changed")
        f.unlink()
        self.assert_blocked("campaign.yaml is missing")

    def test_material_env_change_blocks(self):
        self.approve(self.res)
        self.assert_blocked("material environment changed: FAKE_ENGINE_MODE",
                            env={**self.env, "FAKE_ENGINE_MODE": "fast"})

    def test_material_file_change_blocks(self):
        models = self.tmp / "models.yaml"
        models.write_text("a: 1\n")
        self.env["FAKE_MODELS_FILE"] = str(models)
        self.submit(self.req("req-2", work_id="w-2"))
        res = self.prepare("w-2")
        self.approve(res)
        self.assertTrue(self.reval(res.prep_id).ok)
        models.write_text("a: 2\n")
        rv = self.reval(res.prep_id)
        self.assertTrue(any("$FAKE_MODELS_FILE" in p for p in rv.problems), rv.problems)

    def test_observed_env_drift_warns_only(self):
        self.approve(self.res)
        env = {**self.env, "ANTHROPIC_BASE_URL": "https://other.example",
               "CLAUDE_NEW": "x"}
        del env["CLAUDE_CODE_ENTRYPOINT"]
        rv = self.reval(self.res.prep_id, env)
        self.assertTrue(rv.ok, rv.problems)
        self.assertEqual(sorted(rv.warnings), [
            "$ANTHROPIC_BASE_URL changed since prepare",
            "$CLAUDE_CODE_ENTRYPOINT was set at prepare and is now unset",
            "$CLAUDE_NEW is set now but was not at prepare"])
        self.assertNotIn("other.example", " ".join(rv.warnings))

    def test_new_request_revision_invalidates(self):
        self.approve(self.res)
        self.submit(self.req("req-2", revision=2, question="Changed?"))
        self.assert_blocked("newer request revision")

    def test_material_change_after_approval_leaves_old_approval_unusable(self):
        self.approve(self.res)
        old = self.res
        # The owner changes the question: a new revision, a new preparation.
        self.submit(self.req("req-2", revision=2, question="Changed?"))
        new = self.prepare()
        self.assertNotEqual(new.material_digest, old.material_digest)
        # Old approval: blocked at revalidation...
        self.assertFalse(self.reval(old.prep_id).ok)
        # ...and the new preparation is not covered by it.
        self.assertTrue(any("no approval" in p for p in self.reval(new.prep_id).problems))
        with self.assertRaisesRegex(S.GrantError, "no approval"):
            self.store.reserve_invocation("inv-1", new.prep_id,
                                          expected_digest=new.material_digest)
        # Even the old code against the new preparation is refused.
        with self.assertRaises(AP.ApprovalError):
            AP.approve(self.store, new.prep_id, code=old.short_code, source="chat",
                       quote=go(old.short_code), env=self.env)

    def test_tampered_stored_record_blocks(self):
        self.approve(self.res)
        rec = self.store.get_preparation(self.res.prep_id)["record"]
        rec["material"]["request"]["question"] = "swapped"
        self.store.conn.execute("UPDATE preparations SET record_json = ? WHERE prep_id = ?",
                                (json.dumps(rec), self.res.prep_id))
        self.assert_blocked("does not match its material digest")


class Card(Base):
    def test_card_bounded_and_identical_to_card_md(self):
        long_q = "Why " + "very " * 3000 + "slow?"
        self.submit(self.req(question=long_q, locks={f"k{i}": "v" * 500 for i in range(40)}))
        res = self.prepare()
        self.assertLessEqual(len(res.card.encode("utf-8")), AP.MAX_CARD_BYTES)
        self.assertEqual(res.card_path.read_text(encoding="utf-8"), res.card)
        self.assertIn(res.material_digest, res.card)
        self.assertIn(res.short_code, res.card)
        self.assertIn(f"approve {res.prep_id} --code {res.short_code}", res.card)
        # no raw file contents
        self.assertNotIn("max_iterations: ", res.card)

    def test_card_shows_counts_limits_defaults(self):
        self.submit()
        res = self.prepare()
        for s in ("invocations recorded in this study: 0", "max_iterations (engine): 3",
                  "### Inferred defaults", "experiment: default"):
            self.assertIn(s, res.card)


class CardTruncation(Base):
    """The >MAX_CARD_BYTES branch: the body is dropped, the pointer kept.

    Every card section clips its values (240 chars) and its item count
    (MAX_ITEMS), so a card only crosses the bound when many sections are
    full at once -- the shape a rich adapter produces. The fake adapter's
    single summary line and one limit can never reach it (the existing
    `test_card_bounded_and_identical_to_card_md` builds a 4 KB card), so the
    branch is exercised through an adapter subclass that fills the summary,
    limits and defaults sections, the way the shipped Nous adapter's
    campaign fields would.
    """

    def big_adapter(self):
        class Big(fake.FakeAdapter):
            def prepare(self, request, ctx):
                p = super().prepare(request, ctx)
                p.summary = [f"summary line {i} " + "s" * 220 for i in range(12)]
                p.limits = [{"name": f"lim{i}", "value": "v" * 220,
                             "enforced_by": "engine"} for i in range(12)]
                p.defaults = [{"field": f"f{i}", "value": "d" * 200,
                               "reason": "r" * 30} for i in range(12)]
                return p
        return Big()

    def test_oversize_card_truncates_body_and_keeps_code_and_pointer(self):
        prev = A.register(self.big_adapter(), replace=True)
        self.addCleanup(A.unregister, "nous", prev)
        self.submit(self.req(question="Why " + "very " * 40 + "slow?",
                             locks={f"k{i}": "v" * 230 for i in range(12)}))
        res = self.prepare()
        card = res.card
        # The note's presence is the branch condition: the untruncated card
        # (head + every section + tail) crossed MAX_CARD_BYTES.
        self.assertIn("card truncated to its size bound", card)
        self.assertIn(f"`runtime.py show {res.prep_id} --json`", card)
        # The owner still sees what they are approving: identity, digest, code.
        self.assertTrue(card.startswith(f"## Approval card: {res.prep_id}"))
        self.assertIn(res.material_digest, card)
        self.assertIn(f"approve {res.prep_id} --code {res.short_code}", card)
        # The bound holds after truncation, and card.md stays byte-identical.
        self.assertLessEqual(len(card.encode("utf-8")), AP.MAX_CARD_BYTES)
        self.assertEqual(res.card_path.read_text(encoding="utf-8"), card)
        # The oversized body sections were the ones dropped: the summary lines
        # the big adapter added are gone, but the head sections survive.
        self.assertNotIn("summary line 11", card)
        self.assertIn("### What runs", card)
        self.assertIn("### Semantics and locks", card)
        # ... and the truncated card is still a usable approval: it approves.
        self.approve(res)


class Secrets(Base):
    def test_secret_values_never_in_records_inputs_card_or_logs(self):
        self.env.update({"FAKE_API_TOKEN": SECRET, "ANTHROPIC_API_KEY": SECRET + "x",
                         "CLAUDE_PROXY": f"https://u:{SECRET}@proxy",
                         "FAKE_ENGINE_MODE": f"mode-{SECRET}"})
        sem = {"models": {"session": "model-x"}, "run_id": "r1",
               "secrets": ["FAKE_API_TOKEN", "MISSING_TOKEN"]}
        self.submit(self.req(semantics=sem))
        res = self.prepare()
        rec = self.store.get_preparation(res.prep_id)["record"]
        self.assertEqual(rec["env_secrets"], {"FAKE_API_TOKEN": "present",
                                              "MISSING_TOKEN": "absent"})
        self.assertEqual(rec["env_observed"]["ANTHROPIC_API_KEY"], "present")
        self.assertEqual(rec["env_observed"]["CLAUDE_PROXY"], "redacted")
        self.assertEqual(rec["material"]["env"]["vars"]["FAKE_ENGINE_MODE"], "redacted")
        self.approve(res)
        self.reval(res.prep_id)
        self.store.close()
        blob = b"".join(p.read_bytes() for p in sorted(self.tmp.rglob("*"))
                        if p.is_file() and ".git" not in p.parts)
        self.assertNotIn(SECRET.encode(), blob)
        self.store = S.open_store(self.db)

    def test_adapter_leaking_a_secret_into_inputs_is_refused(self):
        self.env["FAKE_API_TOKEN"] = SECRET
        self.submit()
        orig = self.adapter.prepare

        def leaky(request, ctx):
            p = orig(request, ctx)
            p.files["campaign.yaml"] += f"token: {SECRET}\n".encode()
            return p
        self.adapter.prepare = leaky
        with self.assertRaises(P.PrepareError) as cm:
            self.prepare()
        self.assertNotIn(SECRET, str(cm.exception))
        self.assertIn("FAKE_API_TOKEN", str(cm.exception))
        self.assertFalse(P.prepared_root(self.store).exists()
                         and any(P.prepared_root(self.store).iterdir()))
        self.assertEqual(self.store.list_preparations(), [])

    def test_env_record_rules(self):
        env = {"ANTHROPIC_MODEL": "m1", "ANTHROPIC_AUTH_TOKEN": SECRET,
               "CLAUDE_X": "1", "OTHER": "y", "NOUS_PARENT": "/p"}
        r = envpolicy.env_record(env, material_vars=["NOUS_PARENT", "UNSET_ONE"],
                                 secret_vars=["ANTHROPIC_AUTH_TOKEN"])
        self.assertEqual(r["material"]["vars"], {"NOUS_PARENT": "/p", "UNSET_ONE": None})
        self.assertEqual(r["observed"], {"ANTHROPIC_AUTH_TOKEN": "present",
                                         "ANTHROPIC_MODEL": "m1", "CLAUDE_X": "1"})
        self.assertEqual(r["secrets"], {"ANTHROPIC_AUTH_TOKEN": "present"})
        self.assertNotIn(SECRET, json.dumps(r))


class Cli(Base):
    def run_cli(self, *args):
        env = {k: v for k, v in os.environ.items()
               if not (k.startswith("INVESTIGATION_") or k == "XDG_CONFIG_HOME")}
        env.update(INVESTIGATION_RUNS=str(self.tmp / "runs"),
                   INVESTIGATION_CONFIG_HOME=str(self.tmp / "cfg"),
                   FAKE_API_TOKEN=SECRET)
        study = self.repo / "investigations" / "s1"
        study.mkdir(parents=True, exist_ok=True)
        return subprocess.run([sys.executable, str(FAKE_CLI), "--study", str(study),
                               *args], capture_output=True, text=True, env=env,
                              cwd=self.tmp)

    def test_prepare_then_approve_via_cli(self):
        self.store.close()
        f = self.tmp / "m.json"
        f.write_text(json.dumps(self.req()))
        self.assertEqual(self.run_cli("submit", str(f)).returncode, 0)
        r = self.run_cli("prepare", "w-1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.store = S.open_store(self.db)
        (prep,) = self.store.list_preparations()
        card_md = (P.prep_dir(self.store, prep["prep_id"]) / "card.md").read_text()
        self.assertEqual(r.stdout, card_md)  # printed card is byte-identical
        code = short_code(prep["material_digest"])
        pid = prep["prep_id"]
        outs = [r.stdout + r.stderr]
        for args in (["--code", code, "--source", "chat"],
                     ["--code", "0" * 12, "--source", "chat", "--quote", go(code)],
                     ["--source", "chat", "--quote", go(code)]):
            bad = self.run_cli("approve", pid, *args)
            self.assertEqual(bad.returncode, 1, args)
            outs.append(bad.stdout + bad.stderr)
        ok = self.run_cli("approve", pid, "--code", code, "--source", "chat",
                          "--quote", go(code))
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)["short_code"], code)
        again = self.run_cli("approve", pid, "--code", code, "--source", "chat",
                             "--quote", go(code))
        self.assertEqual(again.returncode, 1)
        outs += [ok.stdout + ok.stderr, again.stdout + again.stderr]
        self.assertNotIn(SECRET, "".join(outs))
        j = self.run_cli("show", pid, "--json")
        self.assertEqual(json.loads(j.stdout)["approval"]["source"], "chat")

    def test_shipped_cli_has_no_fake_adapter(self):
        scripts = HERE.parent / "skills/investigation/scripts"
        for p in (scripts / "ains_runtime").rglob("*.py"):
            self.assertNotIn("FakeAdapter", p.read_text(), p)
        self.assertFalse((scripts / "ains_runtime" / "adapters" / "fake.py").exists())


if __name__ == "__main__":
    unittest.main()
