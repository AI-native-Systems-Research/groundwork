"""preparation.py: the guard branches the R2 suite never reaches.

Measured on main 8d40c69 (CI run 43504194546, unit 3.12; reproduced locally
with COVERAGE_PROCESS_START subprocess tracing, full-suite discover +
combine), preparation.py reads 91% with exactly 23 statements never reached:

  - template()'s list/tuple arm (124) — every existing call site templates
    dicts of scalars; a list inside the material is never walked
  - _request_for_work's work-without-experiment-request refusal (143) — the
    suite covers the no-work case (140) but never a request_message_id that
    points at a non-request message or a missing row
  - _check_rel's refusal (150) — the fake adapter always emits plain names
  - _target_checks' GitStateError wrap (156-157) — no prepare() runs against
    a non-repo
  - prepare()'s prep-dir collision (331-332) and its crash cleanup
    (341-344: writable-again, rmtree, re-raise) — nothing fails between
    mkdir and insert_preparation in the existing tests
  - revalidate's approval-wrong-digest (389), request-message-gone
    (400-401), versions-unavailable (421-423), input-writable-again warning
    (441), GitStateError wrap (445-447), secret-state-change (463) and
    rebuilt-material-differs (475) faces

These tests drive each face directly against the real store, the real
gitstate and the test-only fake adapter (tests/_runtime_fake_adapter.py),
reusing test_runtime_prepare's Base harness. In-process, no subprocess: every
line counts toward coverage.
"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_runtime_prepare as R2  # noqa: E402  (harness: git repo + store + fake adapter)
from ains_runtime import preparation as P  # noqa: E402
from ains_runtime import store as S  # noqa: E402


class Templating(unittest.TestCase):
    def test_lists_and_tuples_are_walked_and_rebuilt(self):
        # The material can carry lists (gitstate's ignored_paths, the card's
        # limits) and tuples (prepared.limits): template must rebuild the
        # container with substituted members, not pass it through untouched.
        non_material = {"runs_dir": "/tmp/runs", "run_id": "r1"}
        obj = {"argv": ["run", "/tmp/runs/r1/campaign.yaml"],
               "limits": ("/tmp/runs/r1/limit", "plain"),
               "nested": [{"dir": "/tmp/runs"}],
               "number": 7}
        out = P.template(obj, non_material)
        self.assertEqual(out["argv"], ["run", "{runs_dir}/{run_id}/campaign.yaml"])
        self.assertEqual(out["limits"], ["{runs_dir}/{run_id}/limit", "plain"])
        self.assertEqual(out["nested"], [{"dir": "{runs_dir}"}])
        self.assertEqual(out["number"], 7)


class RequestGuards(R2.Base):
    def test_work_without_experiment_request_refused(self):
        # A work row whose request_message_id points at a message of another
        # type (a reply, a status) must be refused before any adapter runs.
        self.submit(self.req())
        # A clarification on the same work: submittable, but not a request.
        cl = R2.envelope("clarification", "m-cl", body={"kind": "question",
                                                        "text": "Which trace?"},
                         work_id="w-1", revision=1, reply_to="req-1")
        res = self.store.submit(cl, env=self.env)
        self.assertTrue(res.ok, res.reasons)
        self.store.conn.execute(
            "UPDATE work SET request_message_id = 'm-cl' WHERE work_id = 'w-1'")
        self.store.conn.commit()
        with self.assertRaisesRegex(P.PrepareError, "no recorded experiment-request"):
            self.prepare()
        # The msg-is-None arm of the same guard cannot be reached through
        # this store: work.request_message_id is FK-bound to messages, so a
        # work row can never point at a deleted message. (revalidate()'s
        # sibling face — a record whose request_message_id resolves to
        # nothing — is covered in RevalidateGuards below.)


class InputPathGuard(R2.Base):
    def test_bad_adapter_input_paths_refused(self):
        # _check_rel guards the write target: absolute, parent-escaping and
        # card-clobbering names are refused before anything is written.
        self.submit()
        orig = self.adapter.prepare
        for rel in ("/etc/passwd", "../escape.yaml", "sub/../up.yaml", "card.md",
                    "", "a/b/../../../c"):
            def evil(request, ctx, rel=rel):
                p = orig(request, ctx)
                p.files[rel] = b"x"
                return p
            self.adapter.prepare = evil
            with self.subTest(rel=rel):
                with self.assertRaisesRegex(P.PrepareError, "not a plain relative path"):
                    self.prepare()
                self.assertFalse(P.prepared_root(self.store).exists(),
                                 "no prep dir may be created before the path checks")
        self.adapter.prepare = orig


class NotARepo(R2.Base):
    def test_target_that_is_not_a_git_work_tree_is_refused(self):
        # The GitStateError face: prepare() against a plain directory must
        # surface gitstate's message as PrepareError, not leak the raw
        # exception.
        plain = self.tmp / "plain"
        plain.mkdir()
        self.submit()
        with self.assertRaisesRegex(P.PrepareError, "not inside a git work tree"):
            P.prepare(self.store, "w-1", target=plain, env=self.env)


class PrepDirCollision(R2.Base):
    def test_existing_prep_dir_is_refused_not_overwritten(self):
        # A prep_id whose directory already exists (a crashed earlier
        # prepare, or a hand-made one) must be refused, with the directory's
        # contents left untouched.
        self.submit()
        prep_id = "prep-collision"
        pdir = P.prep_dir(self.store, prep_id)
        pdir.mkdir(parents=True)
        (pdir / "sentinel.txt").write_text("keep me")
        with self.assertRaisesRegex(P.PrepareError, "already exists"):
            self.prepare(prep_id=prep_id)
        self.assertEqual((pdir / "sentinel.txt").read_text(), "keep me")
        self.assertEqual(self.store.list_preparations(), [])


class CrashCleanup(R2.Base):
    def test_insert_failure_cleans_up_and_reraises(self):
        # Between mkdir(pdir) and insert_preparation only a DB failure can
        # strike: the prep dir must be made writable again, removed, and the
        # exception re-raised (never left on disk and never swallowed).
        self.submit()

        def failing_insert(*a, **k):
            raise S.StoreError("boom: disk full")
        with mock.patch.object(self.store, "insert_preparation",
                               side_effect=failing_insert):
            with self.assertRaisesRegex(S.StoreError, "boom"):
                self.prepare()
        # The partial dir is gone (the empty prepared/ root may remain) and
        # nothing was recorded.
        root = P.prepared_root(self.store)
        self.assertEqual(list(root.iterdir()) if root.exists() else [],
                         [], "failed prepare must remove its prep dir")
        self.assertEqual(self.store.list_preparations(), [])
        # The real insert is untouched (mock was context-scoped).
        res = self.prepare()
        self.assertTrue((P.prep_dir(self.store, res.prep_id) / "card.md").exists())

    def test_read_only_files_are_made_writable_again_before_removal(self):
        # The cleanup must first undo _make_read_only (make_writable) and
        # only then rmtree: without it the read-only prep dir would survive
        # on filesystems that refuse to delete read-only files.
        self.submit()
        made_writable = []
        real_mw = P.make_writable
        with mock.patch.object(P, "make_writable",
                               side_effect=lambda root: [made_writable.append(root),
                                                         real_mw(root)][-1]):
            with mock.patch.object(self.store, "insert_preparation",
                                   side_effect=S.StoreError("boom")):
                with self.assertRaisesRegex(S.StoreError, "boom"):
                    self.prepare()
        root = P.prepared_root(self.store)
        self.assertEqual(len(made_writable), 1)
        self.assertEqual(made_writable[0].parent, root)  # make_writable ran on the prep dir
        self.assertEqual(list(root.iterdir()) if root.exists() else [],
                         [], "failed prepare must remove its prep dir")


class RevalidateGuards(R2.Base):
    """revalidate()'s uncovered faces (spec §7 step 6)."""

    def setUp(self):
        super().setUp()
        self.submit()
        self.res = self.prepare()
        self.approve(self.res)

    def assert_problem(self, pattern, env=None):
        rv = self.reval(self.res.prep_id, env)
        self.assertFalse(rv.ok, rv.problems)
        self.assertTrue(any(pattern in p for p in rv.problems), rv.problems)
        return rv

    def test_approval_bound_to_a_different_digest(self):
        # An approval row whose material_digest is not the preparation's
        # (a tampered DB, or an approval moved between preps) blocks launch.
        self.store.conn.execute(
            "UPDATE approvals SET material_digest = ? WHERE prep_id = ?",
            ("sha256:" + "00" * 32, self.res.prep_id))
        self.store.conn.commit()
        self.assert_problem("approval is bound to a different material digest")

    def test_request_message_missing_from_db(self):
        # The request the preparation was built from no longer resolves
        # (the record's request_message_id points at a pruned message, or a
        # hand-migrated DB): revalidate stops right there — everything after
        # that check would compare against unknown material.
        rec = self.store.get_preparation(self.res.prep_id)["record"]
        self.store.conn.execute(
            "UPDATE preparations SET record_json = ? WHERE prep_id = ?",
            (json.dumps({**rec, "request_message_id": "m-gone"}),
             self.res.prep_id))
        self.store.conn.commit()
        rv = self.reval(self.res.prep_id)
        self.assertFalse(rv.ok)
        self.assertIn("the request this preparation was built from is not in this DB",
                      rv.problems)
        # The early return: no further problems are collected.
        self.assertEqual(len(rv.problems), 1, rv.problems)

    def test_versions_unavailable_blocks(self):
        # An engine whose version probe raises must block launch with a
        # problem, never crash it (the caller is about to reserve an
        # invocation).
        def boom(ctx):
            raise RuntimeError("engine probe failed")
        self.adapter.versions = boom
        rv = self.reval(self.res.prep_id)
        self.assertFalse(rv.ok, rv.problems)
        self.assertTrue(any("versions unavailable: engine probe failed" in p
                            for p in rv.problems), rv.problems)

    def test_prepared_input_writable_again_only_warns(self):
        # chmod +w on a prepared input is a warning, not a problem: the
        # bytes are unchanged, so the material still matches.
        f = P.prep_dir(self.store, self.res.prep_id) / "campaign.yaml"
        os.chmod(f, 0o644)
        rv = self.reval(self.res.prep_id)
        self.assertTrue(rv.ok, rv.problems)
        self.assertEqual(rv.warnings, ["prepared input campaign.yaml is writable again"])

    def test_target_gone_between_prepare_and_launch(self):
        # The GitStateError face: the target directory removed after prepare
        # must surface gitstate's message as a problem, not a traceback.
        import shutil
        shutil.rmtree(self.repo)
        rv = self.reval(self.res.prep_id)
        self.assertFalse(rv.ok)
        self.assertTrue(any("not inside a git work tree" in p for p in rv.problems),
                        rv.problems)

    def test_secret_state_change_blocks(self):
        # A campaign secret that was absent at prepare and is present now
        # (or vice versa) is a problem: the record's env_secrets no longer
        # describes reality.
        sem = {"models": {"session": "model-x"}, "run_id": "r1",
               "secrets": ["FAKE_API_TOKEN"]}
        self.submit(self.req("req-2", work_id="w-2", semantics=sem))
        res2 = self.prepare("w-2")
        self.approve(res2)
        # At prepare: FAKE_API_TOKEN is not in env -> recorded "absent".
        self.assertEqual(
            self.store.get_preparation(res2.prep_id)["record"]["env_secrets"],
            {"FAKE_API_TOKEN": "absent"})
        # At launch: present -> state changed -> blocked, naming the variable.
        env = {**self.env, "FAKE_API_TOKEN": "tok-" + "s" * 20}
        rv = self.reval(res2.prep_id, env)
        self.assertFalse(rv.ok, rv.problems)
        self.assertIn("secret $FAKE_API_TOKEN is now present", rv.problems)
        self.assertNotIn("tok-", " ".join(rv.problems))  # value never echoed

    def test_rebuilt_material_differs_blocks(self):
        # Everything else matches, but the material rebuilt from the current
        # state digests differently (an adapter whose prepare output depends
        # on something outside the record): launch must be blocked and name
        # the remedy, without leaking the digest computation.
        real_prepare = self.adapter.prepare

        def drifting(request, ctx):
            p = real_prepare(request, ctx)
            p.material = dict(p.material, engine="drifted-engine")
            return p
        self.adapter.prepare = drifting
        rv = self.reval(self.res.prep_id)
        self.assertFalse(rv.ok, rv.problems)
        self.assertTrue(any("material rebuilt from the current state differs" in p
                            for p in rv.problems), rv.problems)


if __name__ == "__main__":
    unittest.main()
