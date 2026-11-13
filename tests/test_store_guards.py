"""ains_runtime.store: the write-side validation and transition guards.

test_runtime_store.py covers the happy paths and the big grant rules
(consume-once, equivalent material, rollback). The guards exercised here are
the refusal branches around them — bad ids, missing fields, wrong-digest
approvals, unknown invocations, compare-and-set misses, WAL/connect failures
and the schema-statement splitter — which keep malformed or hostile writes
from entering the ledger. All tests are in-process: no subprocess, no
environment scrubbing, so every line counts toward coverage directly.
"""
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runtime_util import envelope, request  # noqa: E402
from ains_runtime import store as S  # noqa: E402

NOENV = {}

# ID_RE: an id must start alphanumeric; anything with a space is refused.
BAD_ID = "not a valid id!"


def prep_record(**material):
    m = {"capability": "nous", "question": "q", "commit": "c" * 40, **material}
    return {"capability": "nous", "material": m, "argv": ["nous", "run", "x"]}


def cancel_msg(message_id, work_id, reply_to, inv_id="inv-1"):
    return envelope("cancel", message_id,
                    {"reason": "owner asked", "invocation_id": inv_id},
                    work_id=work_id, reply_to=reply_to)


class GuardCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = self.tmp / "runs" / ".runtime" / S.DB_NAME
        self.store = S.open_store(self.db)

    def tearDown(self):
        self.store.close()

    def submit(self, msg):
        return self.store.submit(msg, env=NOENV)

    def reserve(self, prep_id="prep-1", work_id="w-1", inv_id="inv-1", **material):
        """One submitted, prepared, approved work item; returns the digest."""
        if self.store.get_work(work_id) is None:
            self.assertTrue(self.submit(request(f"req-{work_id}", work_id=work_id)).ok)
        d = self.store.insert_preparation(prep_id, work_id, prep_record(**material),
                                          env=NOENV)
        self.store.insert_approval(f"appr-{prep_id}", prep_id, d, "chat",
                                   "go, code abc", env=NOENV)
        return d


class SchemaScript(GuardCase):
    def test_trailing_incomplete_statement_is_refused(self):
        # _statements() splits CREATE TRIGGER ... END; as one statement; a
        # fragment left over after the final ';' is a schema bug, not silence.
        good = S._statements(S.SCHEMA_V1)
        self.assertGreater(len(good), 5)
        self.assertTrue(all(s.rstrip().endswith(";") for s in good))
        with self.assertRaisesRegex(S.StoreError, "trailing incomplete"):
            S._statements("CREATE TABLE a(x);\nCREATE TABLE b(y")

    def test_meta_row_is_readable(self):
        self.assertEqual(self.store.meta()["schema_version"],
                         self.store.schema_version())


class SubmitApplicability(GuardCase):
    def test_second_message_for_a_work_cannot_switch_repo(self):
        self.assertTrue(self.submit(request("req-1", work_id="w-1")).ok)
        m = request("req-2", work_id="w-1")
        m["target"]["repo_name"] = "other-repo"
        r = self.submit(m)
        self.assertEqual(r.status, "rejected")
        self.assertIn("belongs to 'demo-repo'", r.reasons[0])
        # The work's repo is unchanged.
        self.assertEqual(self.store.get_work("w-1")["repo_name"], "demo-repo")

    def test_cancel_for_another_works_invocation_is_rejected(self):
        self.assertTrue(self.submit(request("req-1", work_id="w-1")).ok)
        self.assertTrue(self.submit(request("req-2", work_id="w-2")).ok)
        self.reserve()
        self.store.reserve_invocation("inv-1", "prep-1")
        # w-2 cancels w-1's invocation: the body's invocation_id does not
        # belong to w-2, so the message is rejected, not recorded.
        r = self.submit(cancel_msg("can-1", "w-2", "req-2"))
        self.assertEqual(r.status, "rejected")
        self.assertIn("no invocation 'inv-1' for this work", r.reasons[0])
        self.assertIsNone(self.store.get_invocation("inv-1")["cancel_requested_at"])
        # The same cancel for the owning work is accepted (control).
        r2 = self.submit(cancel_msg("can-2", "w-1", "req-1"))
        self.assertEqual(r2.status, "accepted")


class PreparationGuards(GuardCase):
    def setUp(self):
        super().setUp()
        self.assertTrue(self.submit(request()).ok)

    def test_malformed_prep_id_is_refused(self):
        with self.assertRaisesRegex(S.StoreError, "prep_id: must match"):
            self.store.insert_preparation(BAD_ID, "w-1", prep_record(), env=NOENV)

    def test_record_without_a_material_mapping_is_refused(self):
        with self.assertRaisesRegex(S.StoreError, "material mapping"):
            self.store.insert_preparation("prep-1", "w-1",
                                          {"capability": "nous"}, env=NOENV)

    def test_duplicate_prep_id_is_refused(self):
        self.store.insert_preparation("prep-1", "w-1", prep_record(), env=NOENV)
        with self.assertRaisesRegex(S.StoreError, "already exists"):
            self.store.insert_preparation("prep-1", "w-1", prep_record(), env=NOENV)
        # One preparation row only.
        self.assertEqual(len(self.store.list_preparations()), 1)


class ApprovalGuards(GuardCase):
    def setUp(self):
        super().setUp()
        self.assertTrue(self.submit(request()).ok)
        self.digest = self.store.insert_preparation("prep-1", "w-1", prep_record(),
                                                    env=NOENV)

    def test_malformed_approval_id_is_refused(self):
        with self.assertRaisesRegex(S.StoreError, "approval_id: must match"):
            self.store.insert_approval(BAD_ID, "prep-1", self.digest, "chat", "go",
                                       env=NOENV)

    def test_blank_owner_quote_is_refused(self):
        with self.assertRaisesRegex(S.StoreError, "owner_quote: required"):
            self.store.insert_approval("appr-1", "prep-1", self.digest, "chat", "  ",
                                       env=NOENV)

    def test_blank_source_is_refused(self):
        with self.assertRaisesRegex(S.StoreError, "source: required"):
            self.store.insert_approval("appr-1", "prep-1", self.digest, "", "go",
                                       env=NOENV)

    def test_second_approval_for_one_preparation_is_refused(self):
        self.store.insert_approval("appr-1", "prep-1", self.digest, "chat", "go",
                                   env=NOENV)
        with self.assertRaisesRegex(S.GrantError, "already has an approval"):
            self.store.insert_approval("appr-2", "prep-1", self.digest, "chat", "go",
                                       env=NOENV)


class ReservationGuards(GuardCase):
    def setUp(self):
        super().setUp()
        self.digest = self.reserve()

    def test_malformed_inv_id_is_refused(self):
        with self.assertRaisesRegex(S.StoreError, "inv_id: must match"):
            self.store.reserve_invocation(BAD_ID, "prep-1")

    def test_approval_bound_to_a_different_digest_is_refused(self):
        # A row edited out from under the API (or a digest from another
        # preparation): the reservation must refuse, not launch the wrong
        # material.
        self.store.conn.execute(
            "UPDATE approvals SET material_digest = ? WHERE approval_id = ?",
            ("sha256:" + "9" * 64, "appr-prep-1"))
        with self.assertRaisesRegex(S.GrantError, "different material digest"):
            self.store.reserve_invocation("inv-1", "prep-1")
        self.assertIsNone(self.store.get_invocation("inv-1"))

    def test_repeat_of_an_unknown_invocation_is_refused(self):
        with self.assertRaisesRegex(S.GrantError, "no invocation 'inv-none'"):
            self.store.reserve_invocation("inv-1", "prep-1", repeat_of="inv-none")
        # The refusal rolled the approval back to unused.
        self.assertIsNone(self.store.get_approval("prep-1")["consumed_at"])


class TransitionGuards(GuardCase):
    def setUp(self):
        super().setUp()
        self.digest = self.reserve()
        self.store.reserve_invocation("inv-1", "prep-1")

    def test_set_state_on_an_unknown_invocation_is_refused(self):
        with self.assertRaisesRegex(S.StoreError, "no invocation 'inv-none'"):
            self.store.set_invocation_state("inv-none", "running")

    def test_transition_accepts_a_single_state_string(self):
        # from_states may be given as a bare string; it behaves as a 1-tuple.
        self.assertTrue(self.store.transition("inv-1", "reserved", "running"))
        self.assertEqual(self.store.get_invocation("inv-1")["state"], "running")

    def test_transition_rejects_unknown_fields(self):
        with self.assertRaisesRegex(S.StoreError, "unknown invocation fields"):
            self.store.transition("inv-1", ("reserved",), "running", prep_id="x")

    def test_transition_on_an_unknown_invocation_is_refused(self):
        with self.assertRaisesRegex(S.StoreError, "no invocation 'inv-none'"):
            self.store.transition("inv-none", ("reserved",), "running")

    def test_transition_from_a_wrong_state_is_a_clean_false(self):
        # Compare-and-set: the state moved on, so nothing happens and no
        # event is written.
        self.assertFalse(self.store.transition("inv-1", ("spawning",), "exited"))
        self.assertEqual(self.store.get_invocation("inv-1")["state"], "reserved")
        self.assertFalse([e for e in self.store.list_events(entity_id="inv-1")
                          if e["event"] == "state:exited"])

    def test_transition_with_a_wrong_where_value_is_a_clean_false(self):
        self.assertFalse(self.store.transition("inv-1", ("reserved",), "exited",
                                              where={"engine_pid": 999}))
        self.assertEqual(self.store.get_invocation("inv-1")["state"], "reserved")

    def test_record_if_null_rejects_unknown_and_empty_fields(self):
        with self.assertRaisesRegex(S.StoreError, "unknown invocation fields"):
            self.store.record_if_null("inv-1", "evt", prep_id="x")
        with self.assertRaisesRegex(S.StoreError, "unknown invocation fields"):
            self.store.record_if_null("inv-1", "evt")

    def test_record_if_null_on_an_unknown_invocation_is_refused(self):
        with self.assertRaisesRegex(S.StoreError, "no invocation 'inv-none'"):
            self.store.record_if_null("inv-none", "evt", engine_pid=1)

    def test_record_if_null_keeps_the_first_writer(self):
        self.assertTrue(self.store.record_if_null(
            "inv-1", "launch:supervisor_spawned", supervisor_pid=111,
            supervisor_start="ps-lstart:x"))
        # A second writer (the supervisor recording itself after launch's
        # optimistic write) must not overwrite the recorded identity.
        self.assertFalse(self.store.record_if_null(
            "inv-1", "launch:supervisor_spawned", supervisor_pid=222,
            supervisor_start="ps-lstart:y"))
        inv = self.store.get_invocation("inv-1")
        self.assertEqual((inv["supervisor_pid"], inv["supervisor_start"]),
                         (111, "ps-lstart:x"))

    def test_invocation_event_on_an_unknown_invocation_is_refused(self):
        with self.assertRaisesRegex(S.StoreError, "no invocation 'inv-none'"):
            self.store.invocation_event("inv-none", "cancel:result")

    def test_request_cancel_on_an_unknown_invocation_is_refused(self):
        with self.assertRaisesRegex(S.StoreError, "no invocation 'inv-none'"):
            self.store.request_cancel("inv-none")


class OpenFailures(GuardCase):
    """A DB that cannot be opened must surface as StoreError, never as a
    raw sqlite3 error reaching a handler."""

    def test_wal_refusal_closes_the_connection_and_raises(self):
        class Cursor:
            def __init__(self, val):
                self.val = val

            def fetchone(self):
                return (self.val,)

        class Conn:
            def __init__(self):
                self.row_factory = None
                self.closed = False

            def execute(self, sql, *a):
                # journal_mode = WAL is the make-or-break pragma; the other
                # PRAGMAs succeed so execution reaches the check.
                if "journal_mode" in sql.lower():
                    return Cursor("delete")
                return Cursor(None)

            def close(self):
                self.closed = True

        conn = Conn()
        with mock.patch.object(S.sqlite3, "connect", return_value=conn):
            with self.assertRaisesRegex(S.StoreError, "could not enable WAL"):
                S.open_store(self.tmp / "no-wal.db")
        self.assertTrue(conn.closed)  # no connection is left behind

    def test_unopenable_path_raises_store_error(self):
        # A directory is not a database: sqlite itself fails to connect.
        d = self.tmp / "not-a-db"
        d.mkdir()
        with self.assertRaisesRegex(S.StoreError, "unable to open"):
            S.open_store(d)
        # And create=False on a missing path names the file it wanted.
        with self.assertRaisesRegex(S.StoreError, "no runtime DB"):
            S.open_store(self.tmp / "missing.db", create=False)


if __name__ == "__main__":
    unittest.main()
