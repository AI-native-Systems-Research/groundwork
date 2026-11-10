"""ains_runtime.store: idempotent submit, conflicts, schema version, grants."""
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runtime_util import envelope, mutate, request  # noqa: E402
from ains_runtime import STORE_SCHEMA_VERSION  # noqa: E402
from ains_runtime import store as S  # noqa: E402
from ains_runtime.canonical import material_digest  # noqa: E402
from ains_runtime.contracts import validate_message  # noqa: E402

NOENV = {}


def prep_record(**material):
    m = {"capability": "nous", "question": "q", "commit": "c" * 40, **material}
    return {"capability": "nous", "material": m, "argv": ["nous", "run", "x"]}


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "runs" / ".runtime" / S.DB_NAME
        self.store = S.open_store(self.db)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def submit(self, msg):
        return self.store.submit(msg, env=NOENV)

    def approved_prep(self, prep_id="prep-1", work_id="w-1", **material):
        if self.store.get_work(work_id) is None:
            self.assertTrue(self.submit(request(f"req-{work_id}", work_id=work_id)).ok)
        d = self.store.insert_preparation(prep_id, work_id, prep_record(**material),
                                          env=NOENV)
        self.store.insert_approval(f"appr-{prep_id}", prep_id, d, "chat",
                                   "go, code abc", env=NOENV)
        return d


class Open(StoreCase):
    def test_wal_and_meta(self):
        self.assertEqual(self.store.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.store.schema_version(), STORE_SCHEMA_VERSION)

    def test_reopen_keeps_data(self):
        self.submit(request())
        self.store.close()
        self.store = S.open_store(self.db)
        self.assertIsNotNone(self.store.get_work("w-1"))

    def test_newer_schema_refused(self):
        self.store.conn.execute("UPDATE meta SET schema_version = ?",
                                (STORE_SCHEMA_VERSION + 1,))
        self.store.close()
        with self.assertRaisesRegex(S.StoreError, "newer than this runtime"):
            self.store = S.open_store(self.db)
        self.store = S.open_store(self.tmp / "other.db")

    def test_migration_hook_runs_for_older_schema(self):
        self.store.conn.execute("UPDATE meta SET schema_version = 0")
        self.store.close()
        ran = []
        S.MIGRATIONS[0] = lambda c: ran.append(c.execute("SELECT 1").fetchone()[0])
        try:
            self.store = S.open_store(self.db)
        finally:
            del S.MIGRATIONS[0]
        self.assertEqual(ran, [1])
        self.assertEqual(self.store.schema_version(), STORE_SCHEMA_VERSION)

    def test_missing_migration_refused(self):
        self.store.conn.execute("UPDATE meta SET schema_version = 0")
        self.store.close()
        with self.assertRaisesRegex(S.StoreError, "no migration"):
            self.store = S.open_store(self.db)
        self.store = S.open_store(self.tmp / "other.db")

    def test_foreign_db_refused(self):
        other = self.tmp / "foreign.db"
        c = sqlite3.connect(other)
        c.execute("CREATE TABLE t(x)")
        c.commit()
        c.close()
        with self.assertRaisesRegex(S.StoreError, "not an AINS runtime DB"):
            S.open_store(other)

    def test_create_false_on_missing(self):
        with self.assertRaises(S.StoreError):
            S.open_store(self.tmp / "nope.db", create=False)

    def test_events_append_only(self):
        self.submit(request())
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.conn.execute("UPDATE events SET event = 'x'")
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.conn.execute("DELETE FROM events")

    def test_tx_rolls_back(self):
        with self.assertRaises(RuntimeError):
            with self.store.tx():
                self.store.add_event("x", "y", "z")
                raise RuntimeError
        self.assertEqual(self.store.list_events(), [])


class Submit(StoreCase):
    def test_accept_returns_valid_receipt(self):
        r = self.submit(request())
        self.assertEqual(r.status, "accepted")
        self.assertEqual(validate_message(r.receipt, NOENV), [])
        self.assertEqual(r.receipt["reply_to"], "req-1")
        self.assertEqual(r.receipt["body"]["work_state"], "accepted")
        self.assertEqual(self.store.get_work("w-1")["state"], "accepted")

    def test_same_id_same_payload_returns_existing_receipt(self):
        first = self.submit(request())
        # Same payload with different key order and JSON spelling.
        again = json.loads(json.dumps(request(), sort_keys=True, ensure_ascii=True))
        second = self.submit(again)
        self.assertEqual(second.status, "duplicate")
        self.assertTrue(second.ok)
        self.assertEqual(second.receipt, first.receipt)
        self.assertEqual(len(self.store.list_messages()), 1)
        self.assertEqual(sum(e["event"] == "received:experiment-request"
                             for e in self.store.list_events()), 1)

    def test_same_id_different_payload_is_conflict(self):
        self.submit(request())
        other = mutate(request(), lambda m: m["body"].update(question="Something else?"))
        r = self.submit(other)
        self.assertEqual(r.status, "conflict")
        self.assertFalse(r.ok)
        self.assertEqual(r.receipt["body"]["status"], "conflict")
        self.assertEqual(validate_message(r.receipt, NOENV), [])
        stored = self.store.get_message("req-1")["envelope"]
        self.assertEqual(stored["body"]["question"], request()["body"]["question"])

    def test_conflict_across_types(self):
        self.submit(request())
        r = self.submit(envelope("cancel", "req-1", {"reason": "x"}))
        self.assertEqual(r.status, "conflict")

    def test_invalid_is_rejected_and_not_stored(self):
        r = self.submit(mutate(request(), lambda m: m["body"].pop("objective")))
        self.assertEqual(r.status, "rejected")
        self.assertEqual(self.store.list_messages(), [])
        r = self.submit({"schema": "ains.experiment-request/v9"})
        self.assertEqual(r.status, "rejected")
        self.assertIsNone(r.receipt)

    def test_runtime_written_types_cannot_be_submitted(self):
        for t, body in (("progress", {"state": "completed", "at": "2026-11-09T00:00:00Z"}),):
            r = self.submit(envelope(t, "p-1", body))
            self.assertEqual(r.status, "rejected")
        self.assertEqual(self.store.list_messages(), [])

    def test_revision_rules(self):
        self.assertEqual(self.submit(request("a", revision=2)).status, "rejected")
        self.assertTrue(self.submit(request("a")).ok)
        self.assertEqual(self.submit(request("b", revision=1)).status, "rejected")
        self.assertEqual(self.submit(request("b", revision=2, question="Revised?")).status,
                         "accepted")
        self.assertEqual(self.store.get_work("w-1")["revision"], 2)
        stale = envelope("cancel", "x", {"reason": "r"}, revision=1)
        self.assertEqual(self.submit(stale).status, "rejected")

    def test_message_for_unknown_work_rejected(self):
        self.assertEqual(self.submit(envelope("cancel", "x", {"reason": "r"})).status,
                         "rejected")

    def test_repeat_of_must_exist_in_this_db(self):
        r = self.submit(request(repeat_of="inv-from-elsewhere", repeat_reason="flaky"))
        self.assertEqual(r.status, "rejected")

    def test_clarification_round_trip(self):
        self.submit(request())
        q = envelope("clarification", "cq", {"kind": "question", "text": "Which trace?"})
        self.assertTrue(self.submit(q).ok)
        self.assertEqual(self.store.get_work("w-1")["state"], "waiting_for_answer")
        bad = envelope("clarification", "ca0", {"kind": "answer", "text": "x"},
                       reply_to="req-1")
        self.assertEqual(self.submit(bad).status, "rejected")
        a = envelope("clarification", "ca", {"kind": "answer", "text": "trace A"},
                     reply_to="cq")
        self.assertTrue(self.submit(a).ok)
        self.assertEqual(self.store.get_work("w-1")["state"], "accepted")

    def test_cancel_is_recorded_not_acted_on(self):
        self.submit(request())
        r = self.submit(envelope("cancel", "x", {"reason": "owner asked"}))
        self.assertTrue(r.ok)
        self.assertEqual(self.store.get_work("w-1")["state"], "accepted")

    def test_concurrent_identical_submissions_store_once(self):
        results, errors = [], []

        def worker():
            try:
                with S.open_store(self.db) as st:
                    results.append(st.submit(request(), env=NOENV).status)
            except Exception as e:  # pragma: no cover
                errors.append(e)
        ts = [threading.Thread(target=worker) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), ["accepted"] + ["duplicate"] * 7)
        self.assertEqual(len(self.store.list_messages()), 1)


class Grants(StoreCase):
    def test_reserve_consumes_approval_once(self):
        self.approved_prep()
        inv = self.store.reserve_invocation("inv-1", "prep-1")
        self.assertEqual(inv["state"], "reserved")
        self.assertIsNotNone(self.store.get_approval("prep-1")["consumed_at"])
        with self.assertRaises(S.GrantError):
            self.store.reserve_invocation("inv-2", "prep-1")

    def test_no_approval_no_reservation(self):
        self.submit(request())
        self.store.insert_preparation("prep-1", "w-1", prep_record(), env=NOENV)
        with self.assertRaisesRegex(S.GrantError, "no approval"):
            self.store.reserve_invocation("inv-1", "prep-1")
        self.assertEqual(self.store.list_invocations(), [])

    def test_approval_must_match_digest(self):
        self.submit(request())
        self.store.insert_preparation("prep-1", "w-1", prep_record(), env=NOENV)
        with self.assertRaises(S.GrantError):
            self.store.insert_approval("a", "prep-1", "sha256:" + "0" * 64, "chat", "go",
                                       env=NOENV)

    def test_equivalent_material_blocked_by_partial_unique_index(self):
        self.approved_prep("prep-1")
        self.approved_prep("prep-2")  # same material, different prep/message ids
        self.store.reserve_invocation("inv-1", "prep-1")
        with self.assertRaisesRegex(S.GrantError, "equivalent invocation"):
            self.store.reserve_invocation("inv-2", "prep-2")
        # Rolled back: prep-2's approval is still unused.
        self.assertIsNone(self.store.get_approval("prep-2")["consumed_at"])
        # An explicit repeat is allowed.
        self.store.reserve_invocation("inv-2", "prep-2", repeat_of="inv-1")
        # A rejected invocation frees the material digest.
        self.approved_prep("prep-3")
        self.store.set_invocation_state("inv-1", "rejected")
        self.store.reserve_invocation("inv-3", "prep-3")

    def test_different_material_not_blocked(self):
        self.approved_prep("prep-1", question="q1")
        self.approved_prep("prep-2", question="q2")
        self.store.reserve_invocation("inv-1", "prep-1")
        self.store.reserve_invocation("inv-2", "prep-2")
        self.assertEqual(self.store.count_invocations("nous"), 2)

    def test_expected_digest_mismatch(self):
        self.approved_prep()
        with self.assertRaisesRegex(S.GrantError, "changed"):
            self.store.reserve_invocation("inv-1", "prep-1",
                                          expected_digest="sha256:" + "1" * 64)
        self.assertIsNone(self.store.get_approval("prep-1")["consumed_at"])

    def test_preparation_digest_is_recomputed(self):
        self.submit(request())
        rec = {**prep_record(), "material_digest": "sha256:" + "2" * 64}
        with self.assertRaises(S.StoreError):
            self.store.insert_preparation("p", "w-1", rec, env=NOENV)
        d = self.store.insert_preparation("p", "w-1", prep_record(), env=NOENV)
        self.assertEqual(d, material_digest(prep_record()["material"]))

    def test_preparation_needs_work_in_this_db(self):
        with self.assertRaises(S.StoreError):
            self.store.insert_preparation("p", "w-unknown", prep_record(), env=NOENV)

    def test_invocation_fields_whitelisted(self):
        self.approved_prep()
        self.store.reserve_invocation("inv-1", "prep-1")
        self.store.set_invocation_state("inv-1", "running", engine_pid=123,
                                        engine_start="ps-lstart:x")
        self.assertEqual(self.store.get_invocation("inv-1")["engine_pid"], 123)
        with self.assertRaises(S.StoreError):
            self.store.set_invocation_state("inv-1", "running", prep_id="x")


if __name__ == "__main__":
    unittest.main()
