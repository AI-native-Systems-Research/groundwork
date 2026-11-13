"""Secret-value planting: a secret-looking env value never lands in stored
records, receipts or exports (spec §6; plan §4.1)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runtime_util import RUNTIME, envelope, request  # noqa: E402
from ains_runtime import envpolicy  # noqa: E402
from ains_runtime import store as S  # noqa: E402
from ains_runtime.contracts import validate_message  # noqa: E402
from ains_runtime.export import export_study  # noqa: E402

SECRET = "sk-planted-7f3e9a1c55d04b2e"
ENV = {"FAKE_API_TOKEN": SECRET, "HOME": "/home/x", "SHORT_KEY": "abc"}


def all_bytes(root: Path) -> bytes:
    out = b""
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out += p.read_bytes()
    return out


class EnvPolicy(unittest.TestCase):
    def test_secret_names(self):
        # True: the keyword is a whole _-delimited segment (or the whole name),
        # optionally flanked by digits inside the segment (KEY2/2KEY/SECRET1 --
        # the standard rotated/second-credential convention, issue #21).
        for n in ("ANTHROPIC_API_KEY", "GH_TOKEN", "DB_PASSWORD", "X_AUTH",
                  "my_secret", "PROM_TOKEN", "AUTH_TOKEN", "MY_SECRET_VALUE",
                  "API_KEY", "GITHUB_TOKEN", "SECRET", "AWS_SECRET_ACCESS_KEY",
                  "SSH_AUTH_SOCK", "TOKEN_COUNT",
                  "GITHUB_TOKEN2", "API_KEY2", "OPENAI_API_KEY2", "KEY2",
                  "2KEY", "SECRET1", "PASSWORD8", "AUTH7", "ANTHROPIC_KEY2"):
            self.assertTrue(envpolicy.is_secret_name(n), n)
        # False: ordinary names whose *substrings* contain a keyword but whose
        # _-delimited segments never equal one (regression: an unanchored
        # pattern matched 9 of these and made e.g. git identity values
        # forbidden strings in messages, records and exports).
        for n in ("HOME", "PATH", "CLAUDE_CODE_ENTRYPOINT", "NOUS_CAMPAIGN_PARENT",
                  "GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME", "GIT_AUTHOR_EMAIL",
                  "GIT_COMMITTER_EMAIL", "WEBAUTH_URL", "AUTHORIZED_KEYS",
                  "WEBAUTHN", "AUTHORIZATION", "MYTOKEN", "MY-TOKEN",
                  "AUTHOR", "AUTHORS", "KEYSTONE", "MONKEY", "KEYNOTE"):
            self.assertFalse(envpolicy.is_secret_name(n), n)
        # TOKEN123 pins the chosen direction for digit-glued placeholder-style
        # names: fail-closed (secret-named), matching TOKEN_COUNT's accepted
        # behavior. MYTOKEN2 stays False -- the digits must glue to a keyword
        # that is already a whole segment, not rescue a glued substring.
        self.assertTrue(envpolicy.is_secret_name("TOKEN123"))
        self.assertFalse(envpolicy.is_secret_name("MYTOKEN2"))

    def test_short_values_ignored(self):
        self.assertEqual(envpolicy.secret_values(ENV), {"FAKE_API_TOKEN": SECRET})

    def test_findings_never_contain_the_value(self):
        found = envpolicy.find_secret_values({"a": [f"x{SECRET}y"], SECRET: 1}, ENV)
        self.assertEqual(len(found), 2)
        for f in found:
            self.assertNotIn(SECRET, f)
            self.assertIn("$FAKE_API_TOKEN", f)

    def test_redact(self):
        self.assertEqual(envpolicy.redact(f"tok={SECRET}", ENV),
                         "tok=[REDACTED:$FAKE_API_TOKEN]")


class Planting(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "runs" / S.DB_NAME
        self.store = S.open_store(self.db)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assert_nowhere(self):
        self.store.close()
        blob = all_bytes(self.tmp)
        self.assertNotIn(SECRET.encode(), blob)
        self.store = S.open_store(self.db)

    def test_planted_secret_in_message_is_refused_everywhere(self):
        placements = [
            lambda m: m["body"].update(question=f"Why does {SECRET} fail?"),
            lambda m: m["body"]["semantics"].update(token=SECRET),
            lambda m: m["body"]["locks"].update({SECRET: "x"}),
            lambda m: m.update(artifacts=[{"kind": "log", "location": f"/tmp/{SECRET}",
                                           "provisional": True}]),
        ]
        for i, place in enumerate(placements):
            with self.subTest(i):
                m = request(f"req-{i}", work_id=f"w-{i}")
                place(m)
                errs = validate_message(m, ENV)
                self.assertTrue(any("FAKE_API_TOKEN" in e for e in errs), errs)
                res = self.store.submit(m, env=ENV)
                self.assertEqual(res.status, "rejected")
                self.assertIsNone(res.receipt)  # the receipt would echo nothing
                self.assertNotIn(SECRET, json.dumps(res.output()))
        self.assertEqual(self.store.list_messages(), [])
        self.assert_nowhere()

    def test_planted_secret_in_records_is_refused(self):
        self.assertTrue(self.store.submit(request(), env=ENV).ok)
        rec = {"capability": "nous", "material": {"q": "x"},
               "env": {"FAKE_API_TOKEN": SECRET}}
        with self.assertRaises(S.StoreError) as cm:
            self.store.insert_preparation("p-1", "w-1", rec, env=ENV)
        self.assertNotIn(SECRET, str(cm.exception))
        ok = {"capability": "nous", "material": {"q": "x"},
              "env": {"FAKE_API_TOKEN": "present"}}
        d = self.store.insert_preparation("p-1", "w-1", ok, env=ENV)
        with self.assertRaises(S.StoreError):
            self.store.insert_approval("a-1", "p-1", d, "chat", f"go {SECRET}", env=ENV)
        self.assert_nowhere()

    def test_export_has_no_secret(self):
        self.assertTrue(self.store.submit(request(), env=ENV).ok)
        self.assertTrue(self.store.submit(envelope("cancel", "c-1", {"reason": "stop"}),
                                          env=ENV).ok)
        study = self.tmp / "study"
        export_study(self.store, study, env=ENV)
        self.assertTrue((study / "runtime" / "requests" / "req-1.json").is_file())
        self.assert_nowhere()

    def test_export_refuses_when_env_changed_to_match_stored_text(self):
        # A value stored when it wasn't secret becomes one later: export
        # refuses rather than publish it.
        self.assertTrue(self.store.submit(request(objective="minimize p99 TTFT-xyz123"),
                                          env={}).ok)
        with self.assertRaises(S.StoreError) as cm:
            export_study(self.store, self.tmp / "study",
                         env={"LATE_SECRET": "p99 TTFT-xyz123"})
        self.assertNotIn("TTFT-xyz123", str(cm.exception))
        self.assertFalse((self.tmp / "study" / "runtime" / "requests").exists())

    def test_cli_submit_with_planted_env(self):
        m = request(question=f"leak {SECRET}?")
        f = self.tmp / "m.json"
        f.write_text(json.dumps(m))
        env = {**os.environ, "FAKE_API_TOKEN": SECRET}
        r = subprocess.run([sys.executable, str(RUNTIME), "--db", str(self.db),
                            "submit", str(f)], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 1)
        self.assertNotIn(SECRET, r.stdout + r.stderr)
        self.assertIn("FAKE_API_TOKEN", r.stdout)
        f.unlink()
        self.assert_nowhere()


class EventGuards(unittest.TestCase):
    """A secret value in ANY stored event detail is refused, not recorded.

    submit()/insert_preparation()/insert_approval() were always guarded;
    invocation_event() was guarded since the cancel-redaction work. The
    remaining event-family write paths — add_event itself and its four
    invocation wrappers (set_invocation_state, transition, record_if_null,
    request_cancel) — skipped the check, so a cancel reason or transition
    detail carrying a secret value was persisted verbatim into the
    append-only events table (which triggers make impossible to scrub).
    These tests plant a secret through each path and assert the write is
    refused with StoreError, the transaction rolls back, and nothing is
    recorded anywhere on disk."""

    def setUp(self):
        # A fresh store like Planting's, but this class plants its own work
        # item + preparation + approval + reserved invocation: the minimum
        # state the invocation write paths need. No adapter, no engine, no
        # process — everything runs in-process.
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "runs" / S.DB_NAME
        self.store = S.open_store(self.db)
        self.assertTrue(self.store.submit(request(), env=ENV).ok)
        rec = {"capability": "nous", "material": {"q": "x"}}
        d = self.store.insert_preparation("prep-1", "w-1", rec, env=ENV)
        self.store.insert_approval("appr-1", "prep-1", d, "chat", "go, code abc",
                                   env=ENV)
        self.store.reserve_invocation("inv-1", "prep-1")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assert_nowhere(self):
        self.store.close()
        blob = all_bytes(self.tmp)
        self.assertNotIn(SECRET.encode(), blob)
        self.store = S.open_store(self.db)

    def _events(self, inv="inv-1"):
        return [json.dumps(e) for e in self.store.list_events(entity_id=inv)]

    def test_secret_in_add_event_is_refused(self):
        with self.assertRaisesRegex(S.StoreError, "FAKE_API_TOKEN") as cm:
            self.store.add_event("invocation", "inv-1", "note",
                                 detail={"why": f"boom {SECRET}"}, env=ENV)
        self.assertNotIn(SECRET, str(cm.exception))
        self.assertFalse(any(SECRET in e for e in self._events()))
        self.assert_nowhere()

    def test_secret_in_set_invocation_state_is_refused(self):
        with self.assertRaisesRegex(S.StoreError, "FAKE_API_TOKEN"):
            self.store.set_invocation_state("inv-1", "running",
                                            detail={"why": f"boom {SECRET}"}, env=ENV)
        # Rolled back: the state itself never moved.
        self.assertEqual(self.store.get_invocation("inv-1")["state"], "reserved")
        self.assertFalse(any(SECRET in e for e in self._events()))
        self.assert_nowhere()

    def test_secret_in_transition_is_refused(self):
        with self.assertRaisesRegex(S.StoreError, "FAKE_API_TOKEN"):
            self.store.transition("inv-1", ("reserved",), "spawning", env=ENV,
                                  detail={"reason": f"boom {SECRET}"})
        self.assertEqual(self.store.get_invocation("inv-1")["state"], "reserved")
        self.assertFalse(any(SECRET in e for e in self._events()))
        self.assert_nowhere()

    def test_secret_in_record_if_null_fields_is_refused(self):
        with self.assertRaisesRegex(S.StoreError, "FAKE_API_TOKEN"):
            self.store.record_if_null("inv-1", "launch:supervisor_spawned", env=ENV,
                                      native_run_id=f"run-{SECRET}")
        self.assertIsNone(self.store.get_invocation("inv-1")["native_run_id"])
        self.assertFalse(any(SECRET in e for e in self._events()))
        self.assert_nowhere()

    def test_secret_in_request_cancel_is_refused_and_cancel_not_recorded(self):
        with self.assertRaisesRegex(S.StoreError, "FAKE_API_TOKEN"):
            self.store.request_cancel("inv-1", {"reason": f"stop {SECRET}"}, env=ENV)
        # Rolled back: no cancel_requested_at, no cancel:requested event.
        self.assertIsNone(self.store.get_invocation("inv-1")["cancel_requested_at"])
        self.assertFalse(any("cancel:requested" in e for e in self._events()))
        self.assert_nowhere()

    def test_secret_in_cancel_reason_is_refused_through_recovery_cancel(self):
        # recovery.cancel(reason=...) is the owner-facing path that reaches
        # request_cancel; before the fix the cancel:requested event carried
        # the reason verbatim (the adapter result was redacted at
        # recovery.py, but the request event was written first, unredacted).
        import time
        from ains_runtime import recovery as R
        with mock.patch.object(R, "reconcile", side_effect=lambda *a, **k: {
                "terminal": False, "state": "running", "lifecycle": "running",
                "cancel_requested_at": None}):
            with self.assertRaisesRegex(S.StoreError, "FAKE_API_TOKEN"):
                R.cancel(self.store, "inv-1", reason=f"stop {SECRET}", env=ENV)
        self.assertIsNone(self.store.get_invocation("inv-1")["cancel_requested_at"])
        self.assertFalse(any(SECRET in e for e in self._events()))
        self.assert_nowhere()

    def test_clean_events_still_record_with_env_given(self):
        # The guard must not over-refuse: a secret-free detail records fine
        # through every wrapper even with env handed in.
        self.store.set_invocation_state("inv-1", "spawning", env=ENV,
                                        detail={"why": "ordinary reason"})
        self.store.transition("inv-1", ("spawning",), "running", env=ENV,
                              detail={"reason": "engine up"})
        self.store.record_if_null("inv-1", "launch:supervisor_spawned", env=ENV,
                                  supervisor_pid=424242, supervisor_start="x")
        self.store.request_cancel("inv-1", {"reason": "owner asked"}, env=ENV)
        self.assertEqual(self.store.get_invocation("inv-1")["state"], "running")
        self.assertIsNotNone(self.store.get_invocation("inv-1")["cancel_requested_at"])
        self.assertEqual(self.store.get_invocation("inv-1")["supervisor_pid"], 424242)
        evs = [e["event"] for e in self.store.list_events(entity_id="inv-1")]
        for ev in ("state:spawning", "state:running", "launch:supervisor_spawned",
                   "cancel:requested"):
            self.assertIn(ev, evs)
        self.assert_nowhere()


if __name__ == "__main__":
    unittest.main()
