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
import _runtime_fake_adapter as fake  # noqa: E402
from _runtime_util import RUNTIME, envelope, request  # noqa: E402
from ains_runtime import approval as AP  # noqa: E402
from ains_runtime import envpolicy  # noqa: E402
from ains_runtime import preparation as P  # noqa: E402
from ains_runtime import recovery as R  # noqa: E402
from ains_runtime import store as S  # noqa: E402
from ains_runtime import supervisor as SV  # noqa: E402
from ains_runtime.adapters.base import NativeState  # noqa: E402
from ains_runtime.contracts import validate_message  # noqa: E402
from ains_runtime.export import export_study  # noqa: E402
from test_runtime_prepare import git, go, make_repo  # noqa: E402

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


class EngineNativeRedaction(unittest.TestCase):
    """reconcile's classification path REDACTS engine-authored native text
    that quotes a secret value, instead of letting the store's env-keyed
    guard refuse it.

    The engine is spawned with the secret values (supervisor.engine_env
    passes env_pass through, and env_pass_names includes the request's
    secret_vars), so an adapter's classify() — built from engine artifacts
    like halt.json's message — can quote a token in its reason/warnings.
    Before this fix both faces of one root cause:

      wedge: with the secret in the caller's env, the transition was
             REFUSED, the invocation stayed `running` forever, and cancel
             (which reconciles first) was blocked too;
      leak:  with the secret absent (cron, second shell, post-rotation),
             nothing matched, and the token persisted verbatim into
             events.detail_json and invocations.native_json — the
             append-only events table cannot be scrubbed, and export_study
             shipped it to disk.

    The fix runs find_secret_values over the adapter-built native dict
    BEFORE store.transition and replaces offending strings (precedent:
    cancel's redaction of adapter results, recovery.py). The env-keyed
    refusal stays for operator-typed detail (EventGuards above)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        self.repo = make_repo(self.tmp)
        self.commit = git(self.repo, "rev-parse", "HEAD")
        self.db = self.tmp / "runs" / "demo-repo" / "s1" / ".runtime" / S.DB_NAME
        self.store = S.open_store(self.db)
        self.adapter, restore = fake.use()
        self.addCleanup(restore)
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "HOME": str(self.tmp), "CLAUDE_CODE_ENTRYPOINT": "cli",
                    "FAKE_API_TOKEN": SECRET}
        # The engine-shaped classification: reason/warnings quoting the
        # token, exactly what nous.classify builds from halt.json's message.
        self.adapter.classify = lambda invocation, record, ctx: NativeState(
            "failed", "halt", partial=True,
            reason=f"engine log: bearer {SECRET}",
            warnings=[f"engine echoed {SECRET} in a warning"],
            evidence=[{"kind": "fake-halt", "location": f"/tmp/{SECRET}/halt.json"}])

    def _cleanup(self):
        P.make_writable(P.prepared_root(self.store))
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _running_invocation(self):
        """Submit→prepare→approve→reserve→running, engine and supervisor
        both gone: the state reconcile classifies from. No process spawned."""
        m = request("req-1", work_id="w-1")
        m["target"]["commit"] = self.commit
        self.assertTrue(self.store.submit(m, env=self.env).ok)
        res = P.prepare(self.store, "w-1", target=self.repo, env=self.env)
        AP.approve(self.store, res.prep_id, code=res.short_code, source="chat",
                   quote=go(res.short_code), env=self.env)
        self.store.reserve_invocation("inv-1", res.prep_id,
                                      expected_digest=res.material_digest,
                                      launcher=(999998, "1"),
                                      launch={"argv": ["fake-engine", "run"],
                                              "cwd": str(self.tmp), "env_pass": [],
                                              "env_set": {},
                                              "logs": {"engine": str(self.tmp / "e.log")}})
        self.store.transition("inv-1", ("reserved",), SV.SPAWNING,
                              supervisor_pid=999999, supervisor_start="1")
        self.store.transition("inv-1", ("spawning",), SV.RUNNING,
                              engine_pid=999999, engine_start="1")
        return "inv-1"

    def _state_failed_event(self):
        row = self.store.conn.execute(
            "SELECT detail_json FROM events WHERE entity_id='inv-1'"
            " AND event='state:failed'").fetchone()
        return row[0] if row else ""

    def _native_json(self):
        row = self.store.conn.execute(
            "SELECT native_json FROM invocations WHERE inv_id='inv-1'").fetchone()
        return row[0] or ""

    def test_secret_in_env_no_longer_wedges_and_nothing_persists(self):
        # SIDE A (wedge): the secret IS in the caller's env. Before the fix
        # reconcile raised StoreError, the invocation stayed `running`
        # forever, and mark_not_launched was unreachable from that state.
        inv_id = self._running_invocation()
        out = R.reconcile(self.store, inv_id, env=self.env)   # no StoreError
        self.assertEqual(out["state"], "failed")              # terminal, not stuck
        self.assertNotIn(SECRET, self._state_failed_event())  # redacted, kept
        self.assertNotIn(SECRET, self._native_json())
        self.assertIn("redacted: possible credential in engine artifact",
                      self._native_json())
        # The events table keeps a truthful, secret-free record of WHY.
        self.assertIn("state:failed",
                      [e["event"] for e in self.store.list_events(entity_id=inv_id)])

    def test_export_after_redaction_carries_no_token(self):
        # SIDE A continued: the exported invocation file is token-free.
        inv_id = self._running_invocation()
        R.reconcile(self.store, inv_id, env=self.env)
        study = self.tmp / "study"
        export_study(self.store, study, env=self.env)   # would refuse pre-fix
        exported = (study / "runtime" / "invocations" / "inv-1.json").read_text()
        self.assertNotIn(SECRET, exported)
        self.assertIn("redacted: possible credential in engine artifact", exported)

    def test_cancel_on_the_wedged_shape_completes(self):
        # SIDE D: cancel reconciles first; pre-fix it died on the same
        # StoreError and left the run `running`.
        inv_id = self._running_invocation()
        self.store.request_cancel(inv_id, {"reason": "owner wants out"},
                                  env={**self.env, "FAKE_API_TOKEN": SECRET})
        out = R.cancel(self.store, inv_id, reason="owner wants out", env=self.env)
        self.assertEqual(out["state"], "failed")
        self.assertFalse(out["cancel"]["requested"])  # engine already gone
        for e in self.store.list_events(entity_id=inv_id):
            self.assertNotIn(SECRET, json.dumps(e))
            self.assertNotIn(SECRET, e["detail_json"])

    def test_secret_only_in_engine_text_not_env_still_classifies(self):
        # The token in the native text but NOT in the reconcile caller's env
        # (rotation, cron): the guard has nothing to match — the known
        # residual, tracked as the follow-up denylist question in the issue.
        # This pins that the redaction path never corrupts the record:
        # classification proceeds and the state is terminal.
        inv_id = self._running_invocation()
        clean = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "HOME": str(self.tmp), "CLAUDE_CODE_ENTRYPOINT": "cli"}
        out = R.reconcile(self.store, inv_id, env=clean)
        self.assertEqual(out["state"], "failed")

    def test_clean_classification_is_untouched_by_the_redactor(self):
        # Negative control: a secret-free native state records verbatim —
        # no marker, no warning, nothing dropped.
        self.adapter.classify = lambda invocation, record, ctx: NativeState(
            "failed", "halt", reason="halt: repeated identical failures")
        inv_id = self._running_invocation()
        out = R.reconcile(self.store, inv_id, env=self.env)
        self.assertEqual(out["state"], "failed")
        self.assertNotIn("redacted", self._native_json())

    def test_operator_typed_refusals_still_fail_closed(self):
        # Negative control (EventGuards parity): the operator-typed paths the
        # fix must NOT relax. A secret in a cancel reason is REFUSED, not
        # redacted — recovery.cancel reaches store.request_cancel, whose
        # env-keyed guard rolls the whole request back.
        inv_id = self._running_invocation()
        with mock.patch.object(R, "reconcile", side_effect=lambda *a, **k: {
                "terminal": False, "state": "running", "lifecycle": "running",
                "cancel_requested_at": None}):
            with self.assertRaisesRegex(S.StoreError, "FAKE_API_TOKEN"):
                R.cancel(self.store, inv_id, reason=f"stop {SECRET}", env=ENV)
        self.assertIsNone(self.store.get_invocation(inv_id)["cancel_requested_at"])

    # --- numeric-token carriers -------------------------------------------------
    #
    # _redact_native's parsed-tree check (find_secret_values) walks STRINGS
    # only, while the store's guard checks the detail INCLUDING the
    # canonical-serialized native_json, where a JSON number is text: an
    # all-digit secret value (>= MIN_SECRET_LEN) echoed by the engine into a
    # JSON number field passes _redact_native untouched and is still refused
    # by the guard. The reachability is not low: spotlights_static.assess
    # embeds the ENTIRE parsed result.json / run_manifest.json into the
    # native dict (a["result"], a["manifest"] = result, manifest), so any
    # engine-authored number anywhere in those files flows into native_json.
    # Same two faces as the string case above.

    NUMERIC_SECRET = "7331944227184"    # all digits, len 13 >= MIN_SECRET_LEN

    def _numeric_classify(self):
        # run_manifest.json-shaped evidence: a JSON number run_id quoting the
        # token, alongside a JSON null (both survive a parsed-tree walk that
        # only examines strings, and null must pass scrub's fall-through
        # unchanged — engine artifacts are full of null fields).
        self.adapter.classify = lambda invocation, record, ctx: NativeState(
            "failed", "halt", partial=True,
            reason="engine log: manifest truncated",
            evidence=[{"kind": "manifest", "run_id": 7331944227184,
                       "ended_at": None}])

    def _numeric_env(self):
        return {**self.env, "FAKE_NUMERIC_TOKEN": self.NUMERIC_SECRET}

    def test_numeric_secret_no_longer_wedges_or_persists(self):
        # SIDE A (wedge), numeric carrier: the secret IS in the caller's env.
        # Before the fix reconcile raised StoreError on the transition (the
        # token lived in $.native_json as digits), the invocation stayed
        # `running`, and cancel died on the same refusal.
        self._numeric_classify()
        env = self._numeric_env()
        inv_id = self._running_invocation()
        out = R.reconcile(self.store, inv_id, env=env)       # no StoreError
        self.assertEqual(out["state"], "failed")             # terminal, not stuck
        self.assertNotIn(self.NUMERIC_SECRET, self._state_failed_event())
        self.assertNotIn(self.NUMERIC_SECRET, self._native_json())
        self.assertIn("redacted: possible credential in engine artifact",
                      self._native_json())
        self.assertIn("state:failed",
                      [e["event"] for e in self.store.list_events(entity_id=inv_id)])

    def test_numeric_secret_clean_env_still_classifies(self):
        # SIDE B (leak face), numeric carrier: with the secret ABSENT from
        # the reconcile caller's env the guard has nothing to match (the
        # known residual, tracked as the denylist follow-up in the issue);
        # this pins that the numeric scrub never corrupts the record.
        self._numeric_classify()
        inv_id = self._running_invocation()
        clean = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "HOME": str(self.tmp), "CLAUDE_CODE_ENTRYPOINT": "cli"}
        out = R.reconcile(self.store, inv_id, env=clean)
        self.assertEqual(out["state"], "failed")             # classification proceeds

    def test_numeric_secret_cancel_on_wedged_shape_completes(self):
        # SIDE D, numeric carrier: cancel reconciles first; before the fix
        # it died on the same StoreError and left the run `running`.
        self._numeric_classify()
        env = self._numeric_env()
        inv_id = self._running_invocation()
        self.store.request_cancel(inv_id, {"reason": "owner wants out"}, env=env)
        out = R.cancel(self.store, inv_id, reason="owner wants out", env=env)
        self.assertEqual(out["state"], "failed")
        self.assertFalse(out["cancel"]["requested"])   # engine already gone
        for e in self.store.list_events(entity_id=inv_id):
            self.assertNotIn(self.NUMERIC_SECRET, json.dumps(e))
            self.assertNotIn(self.NUMERIC_SECRET, e["detail_json"])

    def test_clean_numbers_pass_through_untouched(self):
        # Negative control, numeric carrier: clean engine-authored numbers
        # (no secret match) record verbatim — no marker, no warning, nothing
        # dropped, and the numbers stay numbers (never stringified).
        env = self._numeric_env()                          # secret present, unmatched
        self.adapter.classify = lambda invocation, record, ctx: NativeState(
            "failed", "halt", partial=True, reason="engine log: manifest truncated",
            evidence=[{"kind": "manifest", "run_id": 42, "pages": [3, 7]}])
        inv_id = self._running_invocation()
        out = R.reconcile(self.store, inv_id, env=env)
        self.assertEqual(out["state"], "failed")
        native = json.loads(self._native_json())
        self.assertEqual(native["evidence"][0]["run_id"], 42)
        self.assertEqual(native["evidence"][0]["pages"], [3, 7])
        self.assertNotIn("redacted", self._native_json())

    def test_bool_and_small_int_native_values_are_not_stringified(self):
        # Negative control for the bool/int distinction the fix must keep:
        # True is a fact, not the number 1 (bool is an int subclass), and a
        # small int can never match (MIN_SECRET_LEN) — both stay typed, not
        # marker-strings, and `partial` survives redaction of other fields.
        env = self._numeric_env()
        self.adapter.classify = lambda invocation, record, ctx: NativeState(
            "failed", "halt", partial=True, reason=f"engine log: bearer {SECRET}",
            evidence=[{"kind": "manifest", "ok": True, "count": 1}])
        inv_id = self._running_invocation()
        out = R.reconcile(self.store, inv_id, env=env)
        self.assertEqual(out["state"], "failed")
        native = json.loads(self._native_json())
        self.assertIs(native["partial"], True)
        self.assertIs(native["evidence"][0]["ok"], True)
        self.assertEqual(native["evidence"][0]["count"], 1)


if __name__ == "__main__":
    unittest.main()
