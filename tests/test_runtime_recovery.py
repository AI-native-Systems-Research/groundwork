"""recovery.py's guard and error branches, in-process (R3, plan §4.3-§4.4).

test_runtime_exec.py drives reconcile/cancel through real fake processes;
this module covers the branches those runs cannot reach without sabotaging
the host: process-lookup failures, adapters that misbehave or leak secrets
into cancel results, and the CLI-level argument guards. Every wait stays
bounded, and nothing here needs a live engine except the cancel-adapter
tests, which use the sleep_until_signalled fixture like the exec tests do.
"""
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
from _runtime_util import kill_matching, request, wait_for  # noqa: E402
from ains_runtime import approval as AP  # noqa: E402
from ains_runtime import preparation as P  # noqa: E402
from ains_runtime import procid  # noqa: E402
from ains_runtime import recovery as R  # noqa: E402
from ains_runtime import store as S  # noqa: E402
from ains_runtime import supervisor as SV  # noqa: E402
from ains_runtime.adapters import register, unregister  # noqa: E402
from ains_runtime.adapters.base import NativeState  # noqa: E402
from test_runtime_prepare import git, go, make_repo  # noqa: E402

HERE = Path(__file__).resolve().parent
FAKE_CLI = HERE / "_runtime_fake_cli.py"
SECRET = "sk-planted-recovery-0f3e2d1c4b5a"


def alive(pid):
    return procid.identity(pid) is not None


class Base(unittest.TestCase):
    """A study DB with the fake adapter, shared by every test below."""

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
                    "HOME": str(self.tmp), "UNRELATED_VAR": "do-not-pass",
                    "CLAUDE_CODE_ENTRYPOINT": "cli", "FAKE_API_TOKEN": SECRET}
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

    def approved(self, engine="succeed", **over):
        """Submit + prepare + approve one request; returns the prep id."""
        self.n += 1
        run_id = f"fakerun{self.n:04d}"
        sem = {"models": {"session": "model-x"}, "run_id": run_id, "experiment": engine,
               "secrets": ["FAKE_API_TOKEN"]}
        m = request(f"req-{self.n}", work_id=f"w-{self.n}", semantics=sem, **over)
        m["target"]["commit"] = self.commit
        self.assertTrue(self.store.submit(m, env=self.env).ok)
        res = P.prepare(self.store, f"w-{self.n}", target=self.repo, env=self.env)
        AP.approve(self.store, res.prep_id, code=res.short_code, source="chat",
                   quote=go(res.short_code), env=self.env)
        return res.prep_id

    def reserved(self):
        """A launch interrupted right after the reservation (crash injection):
        state `reserved`, launcher and supervisor gone, nothing spawned."""
        prep = self.approved("succeed")
        r = subprocess.run(
            [sys.executable, str(FAKE_CLI), "--db", str(self.db), "launch", prep],
            capture_output=True, text=True, timeout=60,
            env={**self.env, SV.TEST_CRASH_AT_VAR: "launch:after_reserve",
                 SV.TEST_CRASH_ARM_VAR: SV.TEST_CRASH_ARM_VALUE})
        self.assertEqual(r.returncode, SV.CRASH_EXIT_CODE, r.stderr)
        (inv,) = self.store.list_invocations()
        self.assertEqual(inv["state"], "reserved")
        return prep, inv["inv_id"]

    def running(self, engine="sleep_until_signalled"):
        """A real launched engine recorded as running; returns (inv_id, run_id)."""
        prep = self.approved(engine)
        res = SV.launch(self.store, prep, env=self.env)
        run_id = f"fakerun{self.n:04d}"
        wait_for(lambda: (self.store.get_invocation(res.inv_id) or
                          {"state": None})["state"] == "running",
                 what="engine running")
        st = self.runs / run_id / "state.json"
        wait_for(st.exists, what="engine ready")
        return res.inv_id, run_id

    def spawn_helper(self, group=False):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                             start_new_session=group)
        self.addCleanup(lambda: (p.kill(), p.wait(timeout=10)))
        return p

    def cli(self, *args, env_extra=None):
        return subprocess.run([sys.executable, str(FAKE_CLI), "--db", str(self.db),
                               *args], capture_output=True, text=True,
                              env={**self.env, **(env_extra or {})}, timeout=60)


class Lifecycle(unittest.TestCase):
    """lifecycle(): the stored state -> reported state map (docstring table)."""

    def inv(self, **over):
        inv = {"state": "running"}
        inv.update(over)
        return inv

    def test_terminal_states_report_themselves(self):
        for s in ("completed", "failed", "cancelled", "recovery_required"):
            self.assertEqual(R.lifecycle(self.inv(state=s)), s)

    def test_rejected_group_includes_not_launched(self):
        for s in ("rejected", "not_launched"):
            self.assertEqual(R.lifecycle(self.inv(state=s)), "rejected")

    def test_cancel_requested_outranks_in_flight_states(self):
        for s in ("reserved", "spawning", "running", "exited"):
            self.assertEqual(R.lifecycle(self.inv(state=s, cancel_requested_at="t")),
                             "cancel_requested")

    def test_in_flight_states(self):
        for s, want in (("reserved", "accepted"), ("spawning", "accepted"),
                        ("running", "running"), ("exited", "running")):
            self.assertEqual(R.lifecycle(self.inv(state=s)), want)


class ReconcileGuards(Base):
    def test_reconcile_unknown_invocation_is_refused(self):
        with self.assertRaisesRegex(R.RecoveryError, "no invocation 'inv-nope'"):
            R.reconcile(self.store, "inv-nope", env=self.env)

    def test_reserved_with_live_launcher_stays_reserved(self):
        helper = self.spawn_helper(group=True)
        prep = self.approved("succeed")
        start = procid.start_time(helper.pid)
        self.assertIsNotNone(start)
        self.store.reserve_invocation(
            "inv-live1", prep, launcher=(helper.pid, start),
            launch={"argv": ["definitely-not-running-xyz"], "cwd": str(self.tmp),
                    "env_set": {}, "native_run_id": None, "env_pass": [],
                    "logs": {"supervisor": str(self.tmp / "s.log"),
                             "engine": str(self.tmp / "e.log")}})
        v = R.reconcile(self.store, "inv-live1", env=self.env)
        self.assertEqual(v["state"], "reserved")
        self.assertEqual(v["lifecycle"], "accepted")

    def test_spawning_with_live_supervisor_stays_spawning(self):
        prep, inv_id = self.reserved()
        self.store.transition(inv_id, ("reserved",), SV.SPAWNING)
        # A live supervisor (the liveness probe says so): launch in progress.
        with mock.patch.object(R, "_supervisor_alive", return_value=True):
            v = R.reconcile(self.store, inv_id, env=self.env)
        self.assertEqual(v["state"], "spawning")
        self.assertEqual(v["lifecycle"], "accepted")

    def test_supervisor_identity_match_is_by_pid_start_and_argv(self):
        # _supervisor_alive with a recorded supervisor_pid: the identity must
        # match pid+start AND the supervisor argv, so a recycled pid with a
        # different program is "gone".
        prep, inv_id = self.reserved()
        helper = self.spawn_helper(group=True)
        start = procid.start_time(helper.pid)
        self.store.transition(inv_id, ("reserved",), SV.SPAWNING,
                              supervisor_pid=helper.pid, supervisor_start=start)
        inv = self.store.get_invocation(inv_id)
        # Real helper, real start, but its argv is NOT the supervisor's argv.
        self.assertFalse(R._supervisor_alive(self.store, inv))
        argv = SV.supervisor_argv(self.store, inv_id)
        self.assertEqual(argv[0], sys.executable)
        self.assertTrue(R.procid.matches(helper.pid, start, None))  # pid+start live

    def test_reserved_with_recorded_supervisor_pid_checks_that_supervisor(self):
        # reserved + supervisor_pid recorded: _launch_in_progress delegates to
        # _supervisor_alive (the launcher identity is no longer consulted).
        prep, inv_id = self.reserved()
        helper = self.spawn_helper(group=True)
        start = procid.start_time(helper.pid)
        self.store.transition(inv_id, ("reserved",), SV.RESERVED,
                              supervisor_pid=helper.pid, supervisor_start=start)
        inv = self.store.get_invocation(inv_id)
        with mock.patch.object(R, "_supervisor_alive", return_value=True) as m:
            self.assertTrue(R._launch_in_progress(self.store, inv))
        m.assert_called_once()

    def test_reserved_without_any_live_process_is_recovery_required(self):
        prep, inv_id = self.reserved()
        v = R.reconcile(self.store, inv_id, env=self.env)
        self.assertEqual(v["lifecycle"], "recovery_required")
        self.assertEqual(v["recovery"]["relaunch"], "never")
        self.assertEqual(v["recovery"]["evidence"]["errors"], [])
        self.assertEqual(v["recovery"]["evidence"]["engine_processes"], [])
        self.assertEqual(v["recovery"]["evidence"]["supervisor_processes"], [])
        self.assertIn("the runtime will not relaunch this", " ".join(v["notes"]))

    def test_running_with_both_gone_and_unclassifiable_stays_running(self):
        # RUNNING, engine+supervisor gone, and classification cannot run
        # (unknown capability): the note says so and the state is unchanged.
        prep, inv_id = self.reserved()
        self.store.transition(inv_id, ("reserved",), SV.RUNNING)
        with mock.patch.object(R.A, "get", side_effect=R.A.AdapterError("no such")):
            v = R.reconcile(self.store, inv_id, env=self.env)
        self.assertEqual(v["state"], "running")
        self.assertTrue(any("classification pending" in n for n in v["notes"]))

    def test_running_that_became_terminal_during_reconcile_keeps_its_state(self):
        # RUNNING, engine+supervisor gone, and by the time reconcile re-reads
        # the row another process already recorded a TERMINAL state: reconcile
        # reports it instead of classifying (no transition, no native_json).
        prep, inv_id = self.reserved()
        self.store.transition(inv_id, ("reserved",), SV.RUNNING)
        real_get = self.store.get_invocation
        calls = {"n": 0}

        def flip_after_first(inv_id_):
            inv = real_get(inv_id_)
            calls["n"] += 1
            if inv and calls["n"] > 1 and inv["state"] == SV.RUNNING:
                flipped = dict(inv)
                flipped["state"] = SV.COMPLETED   # another process got there
                return flipped
            return inv

        with mock.patch.object(self.store, "get_invocation", side_effect=flip_after_first):
            v = R.reconcile(self.store, inv_id, env=self.env)
        self.assertEqual(v["state"], "completed")
        self.assertEqual(v["lifecycle"], "completed")
        self.assertIsNone(v["native"])

    def test_engine_gone_supervisor_alive_stays_running(self):
        # RUNNING, engine gone, supervisor still alive: the supervisor will
        # record the exit, so reconcile leaves the state alone.
        prep, inv_id = self.reserved()
        self.store.transition(inv_id, ("reserved",), SV.RUNNING,
                              engine_pid=99999999,
                              engine_start="proc-stat:notreal:999",
                              native_run_id="fakerun0001")
        with mock.patch.object(R, "_supervisor_alive", return_value=True):
            v = R.reconcile(self.store, inv_id, env=self.env)
        self.assertEqual(v["state"], "running")
        self.assertFalse(v["terminal"])

    def test_reserved_with_live_supervisor_argv_stays_reserved(self):
        # reserved, no supervisor identity recorded, launcher gone, but a
        # process with the supervisor's exact argv is alive: launch in progress.
        prep, inv_id = self.reserved()
        seen = {}

        def fake_find(argv):
            seen["argv"] = list(argv or [])
            return [os.getpid()]
        with mock.patch.object(R.procid, "find_by_argv", side_effect=fake_find):
            v = R.reconcile(self.store, inv_id, env=self.env)
        self.assertEqual(v["state"], "reserved")
        self.assertEqual(v["lifecycle"], "accepted")
        self.assertIn("_supervise", seen["argv"])
        self.assertIn(inv_id, seen["argv"])

    def test_survivors_of_a_recorded_cancel_keep_it_non_terminal(self):
        helper = self.spawn_helper(group=True)
        prep, inv_id = self.reserved()
        # A recorded cancel whose group still holds this helper process.
        self.store.request_cancel(inv_id)
        self.store.invocation_event(inv_id, "cancel:result",
                                    {"result": {"groups": [os.getpgid(helper.pid)]}})
        self.store.transition(inv_id, ("reserved",), SV.RUNNING,
                              engine_pid=99999999,
                              engine_start="proc-stat:notreal:999",
                              native_run_id="fakerun0001")
        v = R.reconcile(self.store, inv_id, env=self.env)
        self.assertEqual(v["lifecycle"], "cancel_requested")
        self.assertFalse(v["terminal"])
        self.assertIn(helper.pid, v["survivors"])
        self.assertTrue(any("cancel not confirmed" in n for n in v["notes"]))

    def test_group_listing_failure_is_reported_not_fatal(self):
        helper = self.spawn_helper(group=True)
        prep, inv_id = self.reserved()
        self.store.request_cancel(inv_id)
        self.store.invocation_event(inv_id, "cancel:result",
                                    {"result": {"groups": [os.getpgid(helper.pid)]}})
        self.store.transition(inv_id, ("reserved",), SV.RUNNING,
                              engine_pid=99999999,
                              engine_start="proc-stat:notreal:999",
                              native_run_id="fakerun0001")
        with mock.patch.object(R.procid, "group_members",
                               side_effect=procid.ProcIdError("ps failed")):
            v = R.reconcile(self.store, inv_id, env=self.env)
        self.assertNotIn("survivors", v)
        self.assertTrue(any("ps failed" in w for w in v["native"]["warnings"]))


class ClassifyGuards(Base):
    def test_bad_native_state_from_adapter_is_refused(self):
        prep, inv_id = self.reserved()
        self.store.set_invocation_state(inv_id, SV.EXITED, exit_code=0,
                                        detail={"supervisor": "recorded the exit"})

        class Bad(fake.FakeAdapter):
            def classify(self, invocation, record, ctx):
                return "completed"      # not a NativeState at all

        bad = Bad()
        prev = register(bad, replace=True)
        self.addCleanup(lambda: unregister(bad.capability, prev))
        v = R.reconcile(self.store, inv_id, env=self.env)
        self.assertEqual(v["state"], "exited")   # unchanged: nothing recorded
        self.assertTrue(any("classification pending" in n and
                            "no valid NativeState" in n for n in v["notes"]))

    def test_cancelled_without_cancel_request_warns(self):
        prep, inv_id = self.reserved()
        self.adapter.classify = (
            lambda invocation, record, ctx: NativeState("cancelled", "stopped"))
        self.store.set_invocation_state(inv_id, SV.EXITED, exit_code=0,
                                        detail={"supervisor": "recorded the exit"})
        v = R.reconcile(self.store, inv_id, env=self.env)
        self.assertEqual(v["state"], "cancelled")
        self.assertTrue(any("recorded no cancel request" in w
                            for w in v["native"]["warnings"]))


class StatusRecover(Base):
    def test_status_by_work_id_reconciles_every_invocation(self):
        prep, inv_id = self.reserved()
        out = R.status(self.store, "w-1", env=self.env)
        self.assertEqual(out["work_id"], "w-1")
        self.assertEqual(out["work_state"], "accepted")
        self.assertEqual([i["inv_id"] for i in out["invocations"]], [inv_id])
        self.assertEqual(out["invocations"][0]["lifecycle"], "recovery_required")

    def test_status_unknown_id_is_refused(self):
        with self.assertRaisesRegex(R.RecoveryError, "no work or invocation 'nope'"):
            R.status(self.store, "nope", env=self.env)

    def test_status_by_invocation_id_reconciles_it(self):
        prep, inv_id = self.reserved()
        out = R.status(self.store, inv_id, env=self.env)
        self.assertEqual(out["inv_id"], inv_id)
        self.assertEqual(out["lifecycle"], "recovery_required")

    def test_recover_reconciles_in_flight_invocations(self):
        prep, inv_id = self.reserved()
        out = R.recover(self.store, env=self.env)
        self.assertEqual([v["inv_id"] for v in out["reconciled"]], [inv_id])
        self.assertEqual(out["reconciled"][0]["state"], "recovery_required")
        self.assertEqual(out["recovery_required"], [inv_id])
        # Again: nothing in flight any more, so nothing to reconcile.
        out2 = R.recover(self.store, env=self.env)
        self.assertEqual(out2["reconciled"], [])
        self.assertEqual(out2["recovery_required"], [inv_id])


class CancelGuards(Base):
    def test_cancel_on_terminal_run_is_not_applicable(self):
        prep = self.approved("succeed")
        res = SV.launch(self.store, prep, env=self.env)
        wait_for(lambda: (self.store.get_invocation(res.inv_id) or
                          {"state": None})["state"] in SV.TERMINAL + (SV.EXITED,),
                 what="engine finished")
        out = R.cancel(self.store, res.inv_id, reason="too late", env=self.env)
        self.assertFalse(out["cancel"]["requested"])
        self.assertTrue(out["cancel"]["note"].startswith("invocation is already"))
        events = [e["event"] for e in
                  self.store.list_events(entity_id=res.inv_id)]
        self.assertIn("cancel:not_applicable", events)

    def test_cancel_on_recovery_required_is_not_applicable(self):
        prep, inv_id = self.reserved()
        R.reconcile(self.store, inv_id, env=self.env)
        out = R.cancel(self.store, inv_id, env=self.env)
        self.assertFalse(out["cancel"]["requested"])
        self.assertIn("already recovery_required", out["cancel"]["note"])
        # The not-applicable branch is the ONLY one that may run here: no
        # cancel request is recorded against a recovery_required invocation.
        self.assertIsNone(self.store.get_invocation(inv_id)["cancel_requested_at"])
        events = [e["event"] for e in self.store.list_events(entity_id=inv_id)]
        self.assertIn("cancel:not_applicable", events)
        self.assertNotIn("cancel:requested", events)
        self.assertNotIn("cancel:result", events)

    def _running_with_cancel_stub(self, cancel_impl):
        """A real running engine whose adapter.cancel is replaced by
        cancel_impl; classify stays honest so the final reconcile works.
        The stub lets the engine finish (writes `go`) so no process leaks."""
        inv_id, run_id = self.running()
        go = self.runs / run_id / "go"

        def stub(invocation, record, ctx, *, interrupt=False):
            go.write_text("")          # let the engine end on its own
            return cancel_impl()
        self.adapter.cancel = stub
        return inv_id

    def _engine_done(self, inv_id):
        wait_for(lambda: not alive(self.store.get_invocation(inv_id)["engine_pid"]),
                 what="engine gone")

    def test_adapter_cancel_exception_is_captured(self):
        inv_id = self._running_with_cancel_stub(
            lambda: (_ for _ in ()).throw(OSError("sigqueue full")))
        out = R.cancel(self.store, inv_id, env=self.env)
        self.assertFalse(out["cancel"]["requested"])
        self.assertIn("adapter cancel failed: sigqueue full", out["cancel"]["error"])
        self._engine_done(inv_id)

    def test_adapter_cancel_non_dict_result_is_captured(self):
        inv_id = self._running_with_cancel_stub(lambda: "stopped?")
        out = R.cancel(self.store, inv_id, env=self.env)
        self.assertFalse(out["cancel"]["requested"])
        self.assertIn("adapter cancel returned no result", out["cancel"]["error"])
        self._engine_done(inv_id)

    def test_adapter_cancel_secret_in_result_is_redacted(self):
        inv_id = self._running_with_cancel_stub(
            lambda: {"requested": True, "detail": f"token {SECRET}"})
        out = R.cancel(self.store, inv_id, env=self.env)
        self.assertTrue(out["cancel"]["requested"])
        self.assertIn("redacted:", out["cancel"]["error"])
        self.assertNotIn(SECRET, json.dumps(out["cancel"]))
        # The stored event must not keep the secret either.
        for e in self.store.list_events(entity_id=inv_id):
            self.assertNotIn(SECRET, json.dumps(e))
        self._engine_done(inv_id)

    def test_cancel_after_exit_records_no_signal(self):
        # EXITED (classification still pending): the engine already ended, so
        # no signal is sent and the note says which state it is in.
        prep, inv_id = self.reserved()
        self.store.set_invocation_state(inv_id, SV.EXITED, exit_code=0,
                                        detail={"supervisor": "recorded the exit"})

        def pending(store, inv, env):
            raise R.RecoveryError("adapter gone")   # classification pending
        with mock.patch.object(R, "_classify", side_effect=pending):
            out = R.cancel(self.store, inv_id, env=self.env)
        self.assertEqual(out["state"], "exited")    # unchanged
        self.assertFalse(out["cancel"]["requested"])
        self.assertEqual(out["cancel"]["mechanism"], "none")
        self.assertIn("engine already exited", out["cancel"]["note"])
        events = [e["event"] for e in self.store.list_events(entity_id=inv_id)]
        self.assertIn("cancel:result", events)

    def test_cancel_while_engine_spawning_sends_no_signal(self):
        helper = self.spawn_helper(group=True)
        prep = self.approved("succeed")
        start = procid.start_time(helper.pid)
        self.store.reserve_invocation(
            "inv-spawn1", prep, launcher=(helper.pid, start),
            launch={"argv": ["definitely-not-running-xyz"], "cwd": str(self.tmp),
                    "env_set": {}, "native_run_id": None, "env_pass": [],
                    "logs": {"supervisor": str(self.tmp / "s.log"),
                             "engine": str(self.tmp / "e.log")}})
        self.store.transition("inv-spawn1", ("reserved",), SV.SPAWNING,
                              supervisor_pid=helper.pid, supervisor_start=start)
        with mock.patch.object(R, "_supervisor_alive", return_value=True):
            out = R.cancel(self.store, "inv-spawn1", env=self.env, spawn_wait_s=0.2)
        self.assertFalse(out["cancel"]["requested"])
        self.assertEqual(out["cancel"]["mechanism"], "none")
        self.assertIn("not started", out["cancel"]["note"])


class MarkNotLaunched(Base):
    def test_quote_is_required(self):
        prep, inv_id = self.reserved()
        R.reconcile(self.store, inv_id, env=self.env)
        for bad in (None, "", "   "):
            with self.assertRaisesRegex(R.RecoveryError, "--quote is required"):
                R.mark_not_launched(self.store, inv_id, quote=bad, env=self.env)

    def test_unknown_invocation_is_refused(self):
        with self.assertRaisesRegex(R.RecoveryError, "no invocation 'inv-x'"):
            R.mark_not_launched(self.store, "inv-x", quote="never ran", env=self.env)

    def test_quote_containing_a_secret_is_refused(self):
        prep, inv_id = self.reserved()
        R.reconcile(self.store, inv_id, env=self.env)
        with self.assertRaisesRegex(R.RecoveryError, "secret"):
            R.mark_not_launched(self.store, inv_id,
                                quote=f"never ran, token {SECRET}", env=self.env)

    def test_live_engine_process_refuses_not_launched(self):
        prep, inv_id = self.reserved()
        R.reconcile(self.store, inv_id, env=self.env)
        engine_argv = (self.store.get_invocation(inv_id).get("launch")
                       or {}).get("argv")

        def fake_find(argv):
            # Only the ENGINE's argv matches a live process here.
            return [4242] if argv == engine_argv else []
        with mock.patch.object(R.procid, "find_by_argv", side_effect=fake_find):
            with self.assertRaisesRegex(R.RecoveryError, "it did launch"):
                R.mark_not_launched(self.store, inv_id, quote="never ran",
                                    env=self.env)
        self.assertEqual(self.store.get_invocation(inv_id)["state"],
                         "recovery_required")

    def test_happy_path_transitions_to_not_launched(self):
        prep, inv_id = self.reserved()
        R.reconcile(self.store, inv_id, env=self.env)
        out = R.mark_not_launched(self.store, inv_id, quote="it never started",
                                  env=self.env)
        self.assertEqual(out["lifecycle"], "rejected")
        self.assertEqual(out["state"], "not_launched")
        self.assertIsNotNone(self.store.get_approval(prep)["consumed_at"])
        # Second attempt: no longer recovery_required.
        with self.assertRaisesRegex(R.RecoveryError, "not recovery_required"):
            R.mark_not_launched(self.store, inv_id, quote="again", env=self.env)

    def test_transition_lost_to_a_concurrent_reconcile(self):
        prep, inv_id = self.reserved()
        R.reconcile(self.store, inv_id, env=self.env)
        real_transition = self.store.transition

        def steal(*a, **k):
            if k.get("detail", {}).get("source") == "chat":
                # A concurrent owner got there first.
                real_transition(inv_id, (SV.RECOVERY_REQUIRED,), SV.NOT_LAUNCHED,
                                ended_at=S.now(), detail={"owner_quote": "first"})
                return False
            return real_transition(*a, **k)

        with mock.patch.object(self.store, "transition", side_effect=steal):
            with self.assertRaisesRegex(S.StoreError, "changed state"):
                R.mark_not_launched(self.store, inv_id, quote="second", env=self.env)
        self.assertEqual(self.store.get_invocation(inv_id)["state"], "not_launched")


class Evidence(Base):
    def test_proc_lookup_failure_is_reported_as_evidence_error(self):
        prep, inv_id = self.reserved()
        with mock.patch.object(R.procid, "find_by_argv",
                               side_effect=procid.ProcIdError("no /proc and ps failed")):
            ev = R.evidence(self.store, self.store.get_invocation(inv_id), self.env)
        self.assertIn("no /proc and ps failed", ev["errors"])

    def test_missing_adapter_reports_artifact_error(self):
        prep, inv_id = self.reserved()
        inv = self.store.get_invocation(inv_id)
        with mock.patch.object(R.A, "get",
                               side_effect=R.A.AdapterError("no such capability")):
            ev = R.evidence(self.store, inv, self.env)
        self.assertEqual(ev["artifacts"], [])
        self.assertTrue(any("artifact evidence unavailable" in e for e in ev["errors"]))


if __name__ == "__main__":
    unittest.main()
