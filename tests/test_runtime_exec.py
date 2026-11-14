"""R3: execution control, limits, recovery, cancellation (plan §4.3-§4.5, §5 R3).

Real (fake) processes: the shipped supervisor runs the fake engines in
tests/fixtures/runtime/fake_engines/. Every wait is bounded (wait_for), and
tearDown SIGKILLs every process whose command line contains this test's
temp-dir name (supervisors, engines, their children).
"""
import contextlib
import inspect
import io
import json
import multiprocessing as mp
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _runtime_fake_adapter as fake  # noqa: E402
from _cov_child_env import covsite_env  # noqa: E402
from _runtime_util import kill_matching, request, wait_for  # noqa: E402
from ains_runtime import adapters as A  # noqa: E402
from ains_runtime import approval as AP  # noqa: E402
from ains_runtime import limits as L  # noqa: E402
from ains_runtime import preparation as P  # noqa: E402
from ains_runtime import procid  # noqa: E402
from ains_runtime import recovery as R  # noqa: E402
from ains_runtime import store as S  # noqa: E402
from ains_runtime import supervisor as SV  # noqa: E402
from test_runtime_prepare import git, go, make_repo  # noqa: E402

HERE = Path(__file__).resolve().parent
FAKE_CLI = HERE / "_runtime_fake_cli.py"
ENGINES = HERE / "fixtures" / "runtime" / "fake_engines"
ARMED = {SV.TEST_CRASH_ARM_VAR: SV.TEST_CRASH_ARM_VALUE}
SECRET = "sk-planted-exec-7a6b5c4d3e2f"


def alive(pid):
    return procid.identity(pid) is not None


class Base(unittest.TestCase):
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
                    "CLAUDE_CODE_ENTRYPOINT": "cli", "FAKE_API_TOKEN": SECRET,
                    **{k: v for k, v in os.environ.items()
                     if k.startswith("COVERAGE_")},
                    **covsite_env()}
        self.n = 0

    def _cleanup(self):
        kill_matching(self.tmp.name)
        SV.reap(timeout=5)
        self.store.close()
        P.make_writable(P.prepared_root(self.store))
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def stop(p):
        p.kill()
        p.wait(timeout=10)

    @property
    def runs(self):
        return self.db.parent.parent

    def approved(self, engine="succeed", *, run_id=None, env=None, **over):
        """Submit + prepare + approve one request; returns the prep id."""
        self.n += 1
        # Distinctive run ids: R2's non-material templating substitutes the
        # value in every string, so a short id like "r1" could also match a
        # random temp path and change the digest.
        run_id = run_id or f"fakerun{self.n:04d}"
        sem = {"models": {"session": "model-x"}, "run_id": run_id, "experiment": engine,
               "secrets": ["FAKE_API_TOKEN"]}
        m = request(f"req-{self.n}", work_id=f"w-{self.n}", semantics=sem, **over)
        m["target"]["commit"] = self.commit
        self.assertTrue(self.store.submit(m, env=env or self.env).ok)
        res = P.prepare(self.store, f"w-{self.n}", target=self.repo, env=env or self.env)
        AP.approve(self.store, res.prep_id, code=res.short_code, source="chat",
                   quote=go(res.short_code), env=env or self.env)
        return res.prep_id

    def launch(self, prep_id, env=None):
        return SV.launch(self.store, prep_id, env=self.env if env is None else env)

    def status(self, inv_id):
        return R.status(self.store, inv_id, env=self.env)

    def until(self, inv_id, pred, what, timeout=15.0):
        last = {}

        def probe():
            last["v"] = self.status(inv_id)
            return last["v"] if pred(last["v"]) else None
        try:
            return wait_for(probe, timeout=timeout, what=what)
        except AssertionError as e:
            raise AssertionError(f"{e}; last status: {json.dumps(last.get('v'))[:3000]}; "
                                 f"events: {[x['event'] for x in self.store.list_events(entity_id=inv_id)]}") from None

    def terminal(self, inv_id, timeout=15.0):
        return self.until(inv_id, lambda v: v["terminal"], "terminal state", timeout)

    def running(self, inv_id, run_id):
        """Wait until the engine is recorded running AND has its handlers up."""
        self.until(inv_id, lambda v: v["state"] == "running", "running")
        st = self.runs / run_id / "state.json"
        wait_for(st.exists, what="engine ready")
        return json.loads(st.read_text())

    def starts(self):
        f = self.runs / "starts.log"
        return f.read_text().splitlines() if f.exists() else []


# --- process identity -------------------------------------------------------

class ProcId(Base):
    def test_start_time_independent_of_caller_locale_and_tz(self):
        code = ("import sys; sys.path.insert(0, %r); from ains_runtime import procid; "
                "print(procid.start_time(%d))" % (str(HERE.parent / "skills/investigation"
                                                      "/scripts"), os.getpid()))
        outs = set()
        for extra in ({}, {"LC_ALL": "fr_FR.UTF-8", "LANG": "fr_FR.UTF-8",
                           "TZ": "Asia/Tokyo"}, {"LC_ALL": "C", "TZ": "America/Denver"}):
            env = {**os.environ, **extra}
            out = subprocess.run([sys.executable, "-c", code], env=env,
                                 capture_output=True, text=True, check=True).stdout
            outs.add(out.strip())
        self.assertEqual(len(outs), 1, outs)
        (val,) = outs
        self.assertEqual(val, procid.start_time(os.getpid()))
        self.assertTrue(val.startswith(("proc-stat:", "ps-lstart:")), val)

    def test_missing_ps_and_proc_blocks_launch(self):
        prep = self.approved()
        with mock.patch.object(procid, "_ps_path", return_value=None), \
                mock.patch.object(procid, "_proc_usable", return_value=False):
            self.assertIsNone(procid.method())
            with self.assertRaises(procid.ProcIdError):
                procid.require()
            with self.assertRaisesRegex(SV.LaunchError, "cannot read process start"):
                self.launch(prep)
        self.assertEqual(self.store.list_invocations(), [])
        self.assertIsNone(self.store.get_approval(prep)["consumed_at"])  # not consumed

    def test_engine_argv_normalizes_the_interpreter_path(self):
        # CPython keeps the spelling the interpreter was invoked with (e.g.
        # ../venv/bin/python) in the parent's sys.executable, while every
        # spawned child normalizes it. argv is part of the material digest,
        # so a preparation built from the raw spelling can never digest-match
        # a revalidation rebuilt in a child ("material rebuilt ... differs").
        # engine_argv() therefore returns the abspath.
        argv = fake.engine_argv("succeed")
        self.assertEqual(argv[0], os.path.abspath(sys.executable), argv)
        self.assertEqual(argv[1], str(ENGINES / "succeed.py"), argv)
        self.assertEqual(fake.engine_argv(None), ["fake-engine"])
        self.assertEqual(fake.engine_argv("no-such-engine"), ["fake-engine"])

        # End to end: a preparation built while sys.executable was spelled
        # with a redundant ".." must still launch under the normalized
        # spelling (what a spawned CLI child process sees), because both
        # sides digest the same abspath.
        exe = os.path.abspath(sys.executable)
        if not Path(exe).parent.name:  # exe directly under / (never in CI)
            self.skipTest("sys.executable has no named parent directory")
        dotted = str(Path(exe).parent / ".." / Path(exe).parent.name / Path(exe).name)
        self.assertEqual(os.path.abspath(dotted), exe)
        with mock.patch.object(sys, "executable", dotted):
            prep = self.approved("succeed")
        res = self.launch(prep)
        st = self.terminal(res.inv_id)
        self.assertEqual(st["state"], "completed", st)

    def test_argv_matching(self):
        argv = [sys.executable, "/x/eng.py", "run", "/p/c.yaml", "--run-id", "r1"]
        self.assertTrue(procid.argv_matches(" ".join(argv), argv))
        self.assertTrue(procid.argv_matches(
            "/usr/local/bin/Python /x/eng.py run /p/c.yaml --run-id r1", argv))
        self.assertFalse(procid.argv_matches(
            " ".join(argv[:-1] + ["r2"]), argv))
        self.assertFalse(procid.argv_matches("/bin/sleep 30", argv))
        self.assertFalse(procid.argv_matches("", argv))

    def test_pid_reused_by_a_different_process_is_not_the_engine(self):
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                                  self.tmp.name], start_new_session=True)
        self.addCleanup(self.stop, other)
        wait_for(lambda: alive(other.pid), what="helper alive")
        real = procid.identity(other.pid)
        engine_argv = [sys.executable, str(ENGINES / "succeed.py"), "run", "c", "--run-id", "x"]
        # Same pid, a different start time -> not the engine.
        self.assertFalse(procid.matches(other.pid, real.start + "-earlier", None))
        # Same pid and start second, different argv -> not the engine.
        self.assertFalse(procid.matches(other.pid, real.start, engine_argv))
        self.assertTrue(procid.matches(other.pid, real.start, None))

        # An invocation "running" with that pid recorded under another
        # identity: status must not treat it as alive, and cancel must not
        # signal it.
        prep = self.approved("succeed")
        inv_id, _, _ = SV.reserve(self.store, prep, env=self.env)
        self.store.transition(inv_id, ("reserved",), "running", engine_pid=other.pid,
                              engine_start=real.start + "-earlier",
                              supervisor_pid=other.pid, supervisor_start="x")
        view = {**self.store.get_invocation(inv_id)}
        view["engine_argv"] = view["launch"]["argv"]
        res = self.adapter.stop_process_tree(view, grace=0.1)
        self.assertIn("different process", res["error"])
        self.assertEqual(res["signalled"], [])
        self.assertTrue(alive(other.pid))
        v = self.status(inv_id)
        self.assertTrue(v["terminal"], v)          # classified from native evidence
        self.assertEqual(v["state"], "failed")     # nothing ran: no native output
        self.assertEqual(v["exit_code"], "unknown")
        self.assertTrue(alive(other.pid))


# --- launch -------------------------------------------------------------------

class Launch(Base):
    def test_succeed_end_to_end(self):
        prep = self.approved("succeed")
        res = self.launch(prep)
        self.assertIn(res.state, ("reserved", "spawning", "running", "exited"))
        v = self.terminal(res.inv_id)
        self.assertEqual((v["state"], v["exit_code"]), ("completed", 0))
        self.assertEqual(v["native"]["native_stop_reason"], "completed")
        self.assertEqual(len(self.starts()), 1)
        logs = v["logs"]
        self.assertFalse(logs["provisional"])
        self.assertTrue(Path(logs["supervisor"]).is_file())
        self.assertTrue(Path(logs["engine"]).is_file())
        self.assertEqual(Path(logs["supervisor"]).parent, self.db.parent / "logs")
        inv = self.store.get_invocation(res.inv_id)
        self.assertTrue(inv["engine_start"] and inv["supervisor_start"])
        self.assertIsNotNone(self.store.get_approval(prep)["consumed_at"])
        # The engine got only allowlisted env + env_set; never test hooks.
        names = json.loads((self.runs / "fakerun0001" / "state.json").read_text())["env_names"]
        self.assertIn("FAKE_PARENT", names)
        self.assertIn("FAKE_API_TOKEN", names)        # campaign secret, by name
        self.assertIn("CLAUDE_CODE_ENTRYPOINT", names)  # D9 observed prefix
        self.assertNotIn("UNRELATED_VAR", names)
        # Secrets never in logs or the DB.
        for f in (logs["supervisor"], logs["engine"]):
            self.assertNotIn(SECRET, Path(f).read_text())
        self.assertNotIn(SECRET.encode(), self.db.read_bytes())

    def test_launch_returns_at_once(self):
        prep = self.approved("sleep_until_signalled")
        t = time.monotonic()
        res = self.launch(prep)
        self.assertLess(time.monotonic() - t, 10)
        self.assertFalse(self.status(res.inv_id)["terminal"])
        self.running(res.inv_id, "fakerun0001")
        (self.runs / "fakerun0001" / "go").write_text("")
        self.assertEqual(self.terminal(res.inv_id)["state"], "completed")

    def test_failures_are_classified_from_native_evidence(self):
        a = self.launch(self.approved("fail")).inv_id
        b = self.launch(self.approved("crash_partial")).inv_id
        va, vb = self.terminal(a), self.terminal(b)
        self.assertEqual((va["state"], va["exit_code"]), ("failed", 1))
        self.assertEqual((vb["state"], vb["exit_code"]), ("failed", 3))
        self.assertTrue(vb["native"]["partial"])
        self.assertTrue((self.runs / "fakerun0002" / "partial.txt").is_file())  # kept

    def test_no_approval_used_approval_and_rejections(self):
        self.n += 1
        m = request("req-x", work_id="w-x",
                    semantics={"models": {"session": "m"}, "run_id": "rx",
                               "experiment": "succeed"})
        m["target"]["commit"] = self.commit
        self.store.submit(m, env=self.env)
        res = P.prepare(self.store, "w-x", target=self.repo, env=self.env)
        with self.assertRaisesRegex(SV.LaunchError, "has no approval"):
            self.launch(res.prep_id)
        prep = self.approved("succeed")
        inv = self.launch(prep).inv_id
        with self.assertRaisesRegex(SV.LaunchError, "already used|already has an invocation"):
            self.launch(prep)
        self.terminal(inv)
        self.assertEqual(len(self.starts()), 1)

    def test_drift_after_approval_blocks_launch(self):
        prep = self.approved("succeed")
        (self.repo / "dirty.txt").write_text("x")
        with self.assertRaisesRegex(SV.LaunchError, "uncommitted"):
            self.launch(prep)
        self.assertEqual(self.store.list_invocations(), [])

    def test_supervisor_never_chains_or_classifies(self):
        import ast
        import textwrap
        tree = ast.parse(textwrap.dedent(inspect.getsource(SV.supervise)))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for forbidden in ("reserve", "reserve_invocation", "launch", "A", "get_adapter",
                          "collect", "spawn_supervisor", "classify", "prepare"):
            self.assertNotIn(forbidden, names, forbidden)


class Concurrency(Base):
    def test_concurrent_equivalent_launches_start_exactly_one_engine(self):
        # Four equivalent requests (different message ids, work ids and
        # non-material run ids): same material digest, four approvals.
        preps = [self.approved("succeed") for _ in range(4)]
        digests = {self.store.get_preparation(p)["material_digest"] for p in preps}
        self.assertEqual(len(digests), 1)
        ctx = mp.get_context("spawn")
        barrier = ctx.Barrier(len(preps))
        q = ctx.Queue()
        procs = [ctx.Process(target=fake.launch_worker,
                             args=(str(self.db), p, self.env, barrier, q))
                 for p in preps]
        for p in procs:
            p.start()
        results = [q.get(timeout=60) for _ in procs]
        for p in procs:
            p.join(timeout=30)
        ok = [r for r in results if r[1] == "ok"]
        self.assertEqual(len(ok), 1, results)
        for r in results:
            if r[1] != "ok":
                self.assertIn("equivalent invocation", r[2])
        (inv,) = self.store.list_invocations()
        self.terminal(inv["inv_id"])
        self.assertEqual(len(self.starts()), 1)
        # Duplicate check and approval consumption are one transaction: the
        # losers' approvals were rolled back with their refused reservation.
        consumed = [p for p in preps if self.store.get_approval(p)["consumed_at"]]
        self.assertEqual(consumed, [inv["prep_id"]])

    def test_reservation_failure_rolls_back_consumption(self):
        a, b = self.approved("succeed"), self.approved("succeed")
        SV.reserve(self.store, a, env=self.env)
        with self.assertRaisesRegex(SV.LaunchError, "equivalent invocation"):
            SV.reserve(self.store, b, env=self.env)
        self.assertIsNone(self.store.get_approval(b)["consumed_at"])
        ev = [e["event"] for e in self.store.list_events()
              if e["entity_id"] in (b, self.store.get_approval(b)["approval_id"])]
        self.assertNotIn("consumed", ev)


# --- crash injection and recovery --------------------------------------------

class Crash(Base):
    def cli(self, *args, env_extra=None):
        env = {**self.env, **(env_extra or {})}
        return subprocess.run([sys.executable, str(FAKE_CLI), "--db", str(self.db), *args],
                              capture_output=True, text=True, env=env, timeout=60)

    def test_hook_needs_both_variables_and_a_known_point(self):
        SV.crash_point("supervisor:start", {SV.TEST_CRASH_AT_VAR: "supervisor:start"})
        SV.crash_point("supervisor:start", {**ARMED, SV.TEST_CRASH_AT_VAR: "nope"})
        SV.crash_point("supervisor:start", {SV.TEST_CRASH_AT_VAR: "supervisor:start",
                                            SV.TEST_CRASH_ARM_VAR: "1"})
        with self.assertRaises(AssertionError):
            SV.crash_point("not-a-point", {})
        self.assertTrue(all(n.startswith("AINS_RUNTIME_TEST_ONLY_")
                            for n in (SV.TEST_CRASH_AT_VAR, SV.TEST_CRASH_ARM_VAR)))

    def test_crash_between_reserve_and_spawn_needs_owner_reconcile(self):
        prep = self.approved("succeed")
        r = self.cli("launch", prep, env_extra={
            **ARMED, SV.TEST_CRASH_AT_VAR: "launch:after_reserve"})
        self.assertEqual(r.returncode, SV.CRASH_EXIT_CODE, r.stderr)
        (inv,) = self.store.list_invocations()
        self.assertEqual(inv["state"], "reserved")
        v = self.status(inv["inv_id"])
        self.assertEqual(v["lifecycle"], "recovery_required")
        self.assertEqual(v["recovery"]["relaunch"], "never")
        # recover / status again: still recovery_required, nothing launched.
        rec = R.recover(self.store, env=self.env)
        self.assertEqual(rec["recovery_required"], [inv["inv_id"]])
        self.assertEqual(self.status(inv["inv_id"])["state"], "recovery_required")
        self.assertEqual(self.starts(), [])
        self.assertIsNotNone(self.store.get_approval(prep)["consumed_at"])
        with self.assertRaisesRegex(SV.LaunchError, "already"):
            self.launch(prep)
        # The owner's reconcile: quote required; approval stays consumed.
        self.assertEqual(self.cli("reconcile", inv["inv_id"], "--mark-not-launched")
                         .returncode, 1)
        ok = self.cli("reconcile", inv["inv_id"], "--mark-not-launched",
                      "--quote", "it never started, mark it")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)["lifecycle"], "rejected")
        self.assertEqual(self.store.get_invocation(inv["inv_id"])["state"], "not_launched")
        self.assertIsNotNone(self.store.get_approval(prep)["consumed_at"])
        with self.assertRaises(SV.LaunchError):
            self.launch(prep)
        # A new preparation + new approval of the same material may launch.
        again = self.launch(self.approved("succeed")).inv_id
        self.assertEqual(self.terminal(again)["state"], "completed")
        self.assertEqual(len(self.starts()), 1)

    def test_supervisor_crash_before_spawn(self):
        prep = self.approved("succeed")
        env = {**self.env, **ARMED, SV.TEST_CRASH_AT_VAR: "supervisor:before_spawn"}
        res = self.launch(prep, env=env)
        self.assertTrue(any("TEST-ONLY" in w for w in res.warnings))
        v = self.until(res.inv_id, lambda v: v["state"] == "recovery_required",
                       "recovery_required")
        self.assertEqual(v["recovery"]["evidence"]["engine_processes"], [])
        self.assertEqual(self.starts(), [])
        events = [e["event"] for e in self.store.list_events(entity_id=res.inv_id)]
        self.assertIn("test_crash_hook_present", events)

    def test_supervisor_crash_after_spawn_keeps_evidence(self):
        prep = self.approved("sleep_until_signalled")
        env = {**self.env, **ARMED, SV.TEST_CRASH_AT_VAR: "supervisor:after_spawn"}
        res = self.launch(prep, env=env)
        wait_for((self.runs / "fakerun0001" / "state.json").exists, what="engine started")
        v = self.until(res.inv_id, lambda v: v["state"] == "recovery_required",
                       "recovery_required")
        ev = v["recovery"]["evidence"]
        self.assertEqual(len(ev["engine_processes"]), 1)      # it DID launch
        self.assertTrue(any(a["kind"] == "fake-state" for a in ev["artifacts"]))
        with self.assertRaisesRegex(R.RecoveryError, "it did launch"):
            R.mark_not_launched(self.store, res.inv_id, quote="go", env=self.env)
        self.assertEqual(len(self.starts()), 1)

    def test_supervisor_crash_after_running_reconciles_to_real_state(self):
        prep = self.approved("sleep_until_signalled")
        env = {**self.env, **ARMED, SV.TEST_CRASH_AT_VAR: "supervisor:after_running"}
        res = self.launch(prep, env=env)
        self.running(res.inv_id, "fakerun0001")
        inv = self.store.get_invocation(res.inv_id)
        wait_for(lambda: not alive(inv["supervisor_pid"]), what="supervisor gone")
        # Engine still alive: stays running, never relaunched.
        self.assertEqual(self.status(res.inv_id)["state"], "running")
        (self.runs / "fakerun0001" / "go").write_text("")
        wait_for(lambda: not alive(inv["engine_pid"]), what="engine done")
        v = self.terminal(res.inv_id)
        self.assertEqual(v["state"], "completed")      # from native evidence
        self.assertEqual(v["exit_code"], "unknown")    # nobody saw the exit
        self.assertTrue(any("exit code unknown" in w for w in v["native"]["warnings"]))
        self.assertEqual(len(self.starts()), 1)

    def test_supervisor_crash_after_exit_classifies(self):
        prep = self.approved("fail")
        env = {**self.env, **ARMED, SV.TEST_CRASH_AT_VAR: "supervisor:after_exit"}
        res = self.launch(prep, env=env)
        v = self.terminal(res.inv_id)
        self.assertEqual((v["state"], v["exit_code"]), ("failed", "unknown"))

    def test_reconcile_refuses_non_recovery_and_secret_quote(self):
        prep = self.approved("succeed")
        inv = self.launch(prep).inv_id
        self.terminal(inv)
        with self.assertRaisesRegex(R.RecoveryError, "not recovery_required"):
            R.mark_not_launched(self.store, inv, quote="ok", env=self.env)
        with self.assertRaisesRegex(R.RecoveryError, "secret"):
            R.mark_not_launched(self.store, inv, quote=f"ok {SECRET}", env=self.env)


# --- cancellation ---------------------------------------------------------------

class Cancel(Base):
    def test_cancel_while_running(self):
        prep = self.approved("sleep_until_signalled")
        inv = self.launch(prep).inv_id
        self.running(inv, "fakerun0001")
        res = R.cancel(self.store, inv, reason="owner asked", env=self.env)
        self.assertTrue(res["cancel"]["requested"])
        self.assertTrue(res["cancel"]["receipt_only"])
        self.assertIn(res["lifecycle"], ("cancel_requested", "cancelled"))
        v = self.terminal(inv)
        self.assertEqual(v["state"], "cancelled")
        self.assertTrue(v["native"]["partial"])
        self.assertEqual(json.loads((self.runs / "fakerun0001" / "final.json").read_text()),
                         {"status": "stopped"})                # artifacts kept

    def test_cancel_after_engine_finished_reports_real_state(self):
        inv = self.launch(self.approved("succeed")).inv_id
        self.assertEqual(self.terminal(inv)["state"], "completed")
        res = R.cancel(self.store, inv, env=self.env)
        self.assertEqual(res["state"], "completed")
        self.assertFalse(res["cancel"]["requested"])
        self.assertIsNone(self.store.get_invocation(inv)["cancel_requested_at"])

    def test_cancel_receipt_is_not_a_stop(self):
        # A cooperative stop (like `nous stop`): requested, but the engine keeps
        # running until it reaches its own boundary.
        self.adapter.cancel = lambda inv, rec, ctx, interrupt=False: {
            "mechanism": "cooperative", "requested": True, "error": None}
        inv = self.launch(self.approved("sleep_until_signalled")).inv_id
        self.running(inv, "fakerun0001")
        res = R.cancel(self.store, inv, env=self.env)
        self.assertEqual(res["lifecycle"], "cancel_requested")
        self.assertFalse(res["terminal"])
        self.assertEqual(self.status(inv)["lifecycle"], "cancel_requested")
        # The engine then finishes normally: its real state is reported.
        (self.runs / "fakerun0001" / "go").write_text("")
        self.assertEqual(self.terminal(inv)["state"], "completed")

    def test_cancel_before_spawn(self):
        prep = self.approved("succeed")
        inv, _, _ = SV.reserve(self.store, prep, env=self.env)
        res = R.cancel(self.store, inv, env=self.env, spawn_wait_s=0.1)
        self.assertEqual(res["lifecycle"], "cancel_requested")
        self.assertIn("not started", res["cancel"]["note"])
        SV.spawn_supervisor(self.store, inv, env=self.env)
        v = self.terminal(inv)
        self.assertEqual(v["state"], "cancelled")
        self.assertEqual(self.starts(), [])

    def test_sigterm_ignored_gets_sigkill(self):
        self.adapter.cancel_grace = 0.3
        inv = self.launch(self.approved("ignore_sigterm")).inv_id
        self.running(inv, "fakerun0001")
        res = R.cancel(self.store, inv, env=self.env)
        sigs = [s["signal"] for s in res["cancel"]["signalled"]]
        self.assertEqual(sigs[0], "SIGTERM")
        self.assertIn("SIGKILL", sigs)
        self.assertEqual(res["cancel"]["survivors"], [])
        v = self.terminal(inv)
        self.assertEqual((v["state"], v["exit_code"]), ("cancelled", -9))

    def test_cancel_reaches_new_session_child(self):
        inv = self.launch(self.approved("new_session_child")).inv_id
        st = self.running(inv, "fakerun0001")
        kids = (st["same_group_child"], st["new_session_child"])
        self.assertTrue(all(alive(k) for k in kids))
        res = R.cancel(self.store, inv, env=self.env)
        self.assertGreaterEqual(len(res["cancel"]["groups"]), 2)
        wait_for(lambda: not any(alive(k) for k in kids), what="children gone")
        self.assertEqual(self.terminal(inv)["state"], "cancelled")

    def test_engine_group_signal_alone_misses_new_session_child(self):
        # The R0 finding, as a fixture: killpg(engine) does not reach it.
        env = {**os.environ, "FAKE_PARENT": str(self.tmp / "r0")}
        eng = subprocess.Popen([sys.executable, str(ENGINES / "new_session_child.py"),
                                "run", "c", "--run-id", "t"], env=env,
                               start_new_session=True)
        self.addCleanup(self.stop, eng)
        st_f = self.tmp / "r0" / "t" / "state.json"
        wait_for(st_f.exists, what="tree up")
        st = json.loads(st_f.read_text())
        os.killpg(eng.pid, signal.SIGTERM)
        eng.wait(timeout=10)
        wait_for(lambda: not alive(st["same_group_child"]), what="same-group child gone")
        self.assertTrue(alive(st["new_session_child"]))
        groups = procid.tree_groups(st["new_session_child"])
        self.assertIn(st["new_session_child"], groups)   # its own group

    def test_survivors_keep_cancel_unconfirmed(self):
        helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                                   self.tmp.name], start_new_session=True)
        self.addCleanup(self.stop, helper)
        inv = self.launch(self.approved("sleep_until_signalled")).inv_id
        self.running(inv, "fakerun0001")
        self.store.request_cancel(inv)
        # A recorded cancel whose groups still hold a live process.
        self.store.invocation_event(inv, "cancel:result",
                                    {"result": {"groups": [helper.pid]}})
        os.killpg(self.store.get_invocation(inv)["engine_pid"], signal.SIGTERM)
        self.until(inv, lambda v: v.get("survivors") == [helper.pid], "survivors")
        v = self.status(inv)
        self.assertEqual(v["lifecycle"], "cancel_requested")
        self.assertFalse(v["terminal"])
        helper.kill()
        helper.wait(timeout=10)
        self.assertEqual(self.terminal(inv)["state"], "cancelled")

    def test_interrupt_sends_sigint_to_engine_only(self):
        inv = self.launch(self.approved("sleep_until_signalled")).inv_id
        self.running(inv, "fakerun0001")
        res = R.cancel(self.store, inv, interrupt=True, env=self.env)
        self.assertEqual(res["cancel"]["mechanism"], "sigint")
        v = self.terminal(inv)
        self.assertEqual((v["state"], v["exit_code"]), ("cancelled", 130))


# --- supervise(): the guard branches ----------------------------------------------
# In-process calls: the test runner imports supervisor.py, so these branches
# are measured directly (no child, no SIGKILL tail). Each test drives one
# guard that the end-to-end launches above never reach, because the state it
# guards against has to be manufactured by hand. A spawned supervisor logs to
# its redirected stdout; here we capture the same stream and assert it.

class Supervise(Base):
    def supervise(self, *args, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = SV.supervise(*args, **kw)
        return rc, buf.getvalue()

    def test_unknown_invocation_returns_2(self):
        rc, log = self.supervise(self.store, "inv-nope", env=self.env)
        self.assertEqual(rc, 2)
        self.assertIn("no reserved invocation 'inv-nope'", log)

    def test_invocation_without_a_launch_spec_returns_2(self):
        prep = self.approved("succeed")
        SV.reserve(self.store, prep, env=self.env, inv_id="inv-nospec")
        # Drop the launch spec the reservation stored: nothing to spawn.
        self.store.conn.execute("UPDATE invocations SET launch_json = NULL WHERE "
                                "inv_id = ?", ("inv-nospec",))
        self.store.conn.commit()
        rc, log = self.supervise(self.store, "inv-nospec", env=self.env)
        self.assertEqual(rc, 2)
        self.assertIn("no reserved invocation", log)

    def test_invocation_owned_by_another_supervisor_returns_1(self):
        prep = self.approved("succeed")
        inv_id, _, _ = SV.reserve(self.store, prep, env=self.env,
                                  inv_id="inv-owned")
        self.store.transition(inv_id, (SV.RESERVED,), SV.RESERVED,
                              supervisor_pid=999999, supervisor_start="x")
        rc, log = self.supervise(self.store, inv_id, env=self.env)
        self.assertEqual(rc, 1)
        self.assertIn("belongs to supervisor 999999", log)
        # The reservation is untouched: not consumed by a refusal.
        self.assertEqual(self.store.get_invocation(inv_id)["state"], SV.RESERVED)
        self.assertEqual(self.starts(), [])

    def test_invocation_past_reserved_returns_1(self):
        prep = self.approved("succeed")
        inv_id, _, _ = SV.reserve(self.store, prep, env=self.env,
                                  inv_id="inv-past")
        self.store.transition(inv_id, (SV.RESERVED,), SV.EXITED, exit_code=0,
                              ended_at=S.now())
        rc, log = self.supervise(self.store, inv_id, env=self.env)
        self.assertEqual(rc, 1)
        self.assertIn("is exited, not reserved", log)
        self.assertEqual(self.store.get_invocation(inv_id)["state"], SV.EXITED)
        self.assertEqual(self.starts(), [])

    def test_unspawnable_engine_marks_the_invocation_failed(self):
        prep = self.approved("succeed")
        inv_id, _, _ = SV.reserve(self.store, prep, env=self.env,
                                  inv_id="inv-unspawnable")
        spec = self.store.get_invocation(inv_id)["launch"]
        spec["argv"] = ["/nonexistent/engine", "run"]   # Popen raises OSError
        # supervise() writes the engine log itself; spawn_supervisor normally
        # creates the dir, so do it by hand for this in-process call.
        SV.logs_dir(self.store).mkdir(parents=True, exist_ok=True)
        from ains_runtime.canonical import canonical_json
        self.store.conn.execute("UPDATE invocations SET launch_json = ? WHERE "
                                "inv_id = ?", (canonical_json(spec), "inv-unspawnable"))
        self.store.conn.commit()
        rc, log = self.supervise(self.store, inv_id, env=self.env)
        self.assertEqual(rc, 1)
        self.assertIn("engine spawn failed", log)
        inv = self.store.get_invocation(inv_id)
        self.assertEqual(inv["state"], SV.FAILED)
        # The reason is an event detail; the native record says what failed.
        events = [e for e in self.store.list_events(entity_id=inv_id)
                  if e["event"] == f"state:{SV.FAILED}"]
        self.assertEqual(events[-1]["detail"]["reason"], "engine spawn failed")
        self.assertIn("engine spawn failed", inv["native"]["reason"])
        self.assertEqual(inv["native"]["state"], SV.FAILED)
        self.assertEqual(self.starts(), [])

    def test_cancel_requested_before_spawn_cancels_without_starting(self):
        prep = self.approved("succeed")
        inv_id, _, _ = SV.reserve(self.store, prep, env=self.env,
                                  inv_id="inv-precancel")
        SV.logs_dir(self.store).mkdir(parents=True, exist_ok=True)
        self.store.request_cancel(inv_id, {"reason": "test"})
        rc, log = self.supervise(self.store, inv_id, env=self.env)
        self.assertEqual(rc, 0)
        self.assertIn("cancel requested before spawn; engine not started", log)
        inv = self.store.get_invocation(inv_id)
        self.assertEqual(inv["state"], SV.CANCELLED)
        events = [e for e in self.store.list_events(entity_id=inv_id)
                  if e["event"] == f"state:{SV.CANCELLED}"]
        self.assertEqual(events[-1]["detail"]["reason"],
                         "cancel requested before the engine was spawned; "
                         "nothing ran")
        self.assertEqual(self.starts(), [])

    def test_engine_spawned_out_of_band_records_an_event_not_a_state(self):
        prep = self.approved("succeed")
        inv_id, _, _ = SV.reserve(self.store, prep, env=self.env,
                                  inv_id="inv-oob")
        SV.logs_dir(self.store).mkdir(parents=True, exist_ok=True)
        # The engine races the supervisor into `running`: the transition to
        # SPAWNING must succeed, the one to RUNNING must not, and the pid is
        # recorded as an event instead.
        real_transition = self.store.transition

        def racing_transition(inv, from_states, state, **kw):
            if state == SV.SPAWNING:
                if not real_transition(inv, from_states, state, **kw):
                    return False
                # Another actor wins the RUNNING hop before the engine starts.
                real_transition(inv, (SV.SPAWNING,), SV.RUNNING,
                                engine_pid=424242, engine_start="start-x")
                return True
            if state == SV.RUNNING:
                return real_transition(inv, (SV.SPAWNING,), state, **kw)
            return real_transition(inv, from_states, state, **kw)

        with mock.patch.object(self.store, "transition", racing_transition):
            rc, log = self.supervise(self.store, inv_id, env=self.env)
        self.assertEqual(rc, 0)
        self.assertIn("state already changed; not recorded", log)
        inv = self.store.get_invocation(inv_id)
        events = [e["event"] for e in self.store.list_events(entity_id=inv_id)]
        self.assertIn("engine_spawned_out_of_band", events)
        # The out-of-band winner's identity stands; the engine's own exit is
        # not recorded on top of it (the where-clause on engine_pid fails).
        self.assertEqual(inv["engine_pid"], 424242)
        self.assertEqual(inv["state"], SV.RUNNING)
        self.assertIsNone(inv["exit_code"])
        self.assertNotIn(f"state:{SV.EXITED}", events)

    def test_main_supervise_reports_an_unopenable_db(self):
        # The private CLI entry: a db that does not exist is a usage error
        # (exit 2), not a traceback. Runs as a subprocess like launch does,
        # with the same env, so it is measured via the COVERAGE_* pass-through.
        env = {**self.env, "AINS_RUNTIME_TEST_ONLY_CRASH_AT": ""}
        r = subprocess.run([sys.executable, str(FAKE_CLI), "--db",
                            str(self.tmp / "no-such" / "db.sqlite"),
                            "_supervise", "inv-nope"],
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("_supervise:", r.stderr)


# --- limits -----------------------------------------------------------------------

class Limits(unittest.TestCase):
    def rec(self, cap="nous", limits=None, labelled=()):
        return {"capability": cap, "material": {
            "adapter": {"capability": cap}, "limits": list(labelled),
            "request": {"limits": {"max_iterations": 3} if limits is None else limits}}}

    def test_required_bound(self):
        self.assertEqual(L.launch_problems(self.rec()), [])
        for bad in ({}, {"max_iterations": 0}, {"max_iterations": True},
                    {"max_iterations": "3"}):
            self.assertTrue(L.launch_problems(self.rec(limits=bad)), bad)
        self.assertEqual(L.launch_problems(self.rec("spotlights-static", limits={})), [])

    def test_labels_never_over_promise(self):
        bad = [{"name": "max_cost_usd", "value": 5, "enforced_by": "engine"},
               {"name": "wall_clock_hours", "value": 2, "enforced_by": "runtime"},
               {"name": "x", "value": 1, "enforced_by": "magic"}]
        self.assertEqual(len(L.label_problems(bad)), 3)
        ok = [{"name": "max_cost_usd", "value": 5, "enforced_by": "advisory"},
              {"name": "max_iterations", "value": 3, "enforced_by": "engine"}]
        self.assertEqual(L.label_problems(ok), [])

    def test_unlabelled_request_limits_are_advisory_and_time_is_reported(self):
        rec = self.rec(limits={"max_iterations": 3, "wall_clock_hours": 1,
                               "budget_usd_advisory": 20},
                       labelled=[{"name": "max_iterations", "value": 3,
                                  "enforced_by": "engine"}])
        lab = {x["name"]: x["enforced_by"] for x in L.labelled(rec)}
        self.assertEqual(lab, {"max_iterations": "engine", "wall_clock_hours": "advisory",
                               "budget_usd_advisory": "advisory"})
        inv = {"created_at": "2026-11-09T10:00:00.000000Z",
               "ended_at": "2026-11-09T12:00:00.000000Z"}
        rep = L.elapsed_report(inv, rec)
        self.assertEqual(rep["elapsed_s"], 7200.0)
        (b,) = rep["time_budgets"]
        self.assertEqual((b["budget_s"], b["exceeded"]), (3600.0, True))
        self.assertEqual(L.spend_unknown("x")["reported_usd"], None)

    def test_no_count_caps(self):
        self.assertFalse(any("count" in g["name"] or "max_invocations" in g["name"]
                             for g in L.RUNTIME_GUARDS))


# --- CLI ------------------------------------------------------------------------------

class Cli(Base):
    def cli(self, *args):
        env = {**self.env}
        return subprocess.run([sys.executable, str(FAKE_CLI), "--db", str(self.db), *args],
                              capture_output=True, text=True, env=env, timeout=60)

    def test_launch_status_cancel_recover_via_cli(self):
        prep = self.approved("sleep_until_signalled")
        r = self.cli("launch", prep)
        self.assertEqual(r.returncode, 0, r.stderr)
        inv = json.loads(r.stdout)["inv_id"]
        self.running(inv, "fakerun0001")
        s = self.cli("status", "w-1")
        self.assertEqual(s.returncode, 0, s.stderr)
        self.assertEqual(json.loads(s.stdout)["invocations"][0]["lifecycle"], "running")
        c = self.cli("cancel", inv, "--reason", "owner asked")
        self.assertEqual(c.returncode, 0, c.stderr)
        self.assertIn(json.loads(c.stdout)["lifecycle"], ("cancel_requested", "cancelled"))
        wait_for(lambda: json.loads(self.cli("status", inv).stdout)["terminal"],
                 what="terminal via cli")
        rv = self.cli("recover")
        self.assertEqual(json.loads(rv.stdout)["recovery_required"], [])
        self.assertEqual(self.cli("status", "nope").returncode, 1)
        self.assertEqual(self.cli("launch", prep).returncode, 1)
        for out in (r, s, c, rv):
            self.assertNotIn(SECRET, out.stdout + out.stderr)

    def test_private_supervise_is_hidden(self):
        h = self.cli("--help")
        self.assertNotIn("_supervise", h.stdout)
        self.assertEqual(self.cli("_supervise", "inv-nope").returncode, 2)


class EngineEnv(unittest.TestCase):
    def test_ca_and_proxy_vars_pass_and_others_do_not(self):
        env = {"NODE_EXTRA_CA_CERTS": "/ca.pem", "SSL_CERT_FILE": "/ca.pem",
               "HTTPS_PROXY": "http://proxy", "UNRELATED_VAR": "x",
               "AINS_RUNTIME_TEST_ONLY_CRASH_AT": "supervisor:start"}
        out = SV.engine_env({"env_pass": SV.env_pass_names({}, env)}, env)
        self.assertEqual(out, {"NODE_EXTRA_CA_CERTS": "/ca.pem", "SSL_CERT_FILE": "/ca.pem",
                               "HTTPS_PROXY": "http://proxy"})


# --- reserve()/spawn_supervisor() launch-side guards ------------------------------
# In-process, like the Supervise class: these guards cannot fire in a normal
# launch (the contract validators reject bad request limits at submit and the
# shipped adapters always return well-formed specs), so each one is driven by
# a FakeAdapter subclass or a patched helper that manufactures the defect the
# guard exists for. Every refusal must be a LaunchError naming the reason,
# never a traceback; the spawn failure must leave a reconcilable invocation.

class LaunchGuards(Base):
    """reserve()'s validation of what the adapter returned, and the launch
    problems the stored preparation's own labels can still carry."""

    def reserved(self, spec, *, label="x"):
        """One approved preparation whose adapter returns `spec` from
        execute(); returns whatever reserve() does with it."""
        adapter = fake.FakeAdapter()

        def execute(record, ctx):
            if isinstance(spec, Exception):
                raise spec
            return spec
        adapter.execute = execute
        prev = A.register(adapter, replace=True)
        self.addCleanup(A.unregister, adapter.capability, prev)
        prep = self.approved("succeed")
        return SV.reserve(self.store, prep, env=self.env)

    def test_well_formed_spec_reserves(self):
        # Control: the same subclass machinery with a good spec reserves
        # cleanly, so the refusals below are the guards, not the harness.
        inv_id, spec, _ = self.reserved(
            A.LaunchSpec(argv=["/bin/true", "run"], cwd="/tmp", env_set={}))
        self.assertEqual(self.store.get_invocation(inv_id)["state"], SV.RESERVED)

    def test_execute_returning_a_non_launchspec_is_refused(self):
        with self.assertRaisesRegex(SV.LaunchError,
                                    "adapter.execute did not return a LaunchSpec"):
            self.reserved("not-a-launchspec")

    def test_execute_returning_an_empty_argv_is_refused(self):
        with self.assertRaisesRegex(SV.LaunchError,
                                    "argv must be a non-empty list of strings"):
            self.reserved(A.LaunchSpec(argv=[], cwd="/tmp", env_set={}))

    def test_execute_returning_a_non_string_argv_element_is_refused(self):
        with self.assertRaisesRegex(SV.LaunchError,
                                    "argv must be a non-empty list of strings"):
            self.reserved(A.LaunchSpec(argv=["/bin/true", 42], cwd="/tmp", env_set={}))

    def test_execute_returning_a_relative_cwd_is_refused(self):
        with self.assertRaisesRegex(SV.LaunchError,
                                    "launch cwd must be an absolute path"):
            self.reserved(A.LaunchSpec(argv=["/bin/true"], cwd="rel", env_set={}))

    def test_execute_returning_a_non_string_env_value_is_refused(self):
        with self.assertRaisesRegex(SV.LaunchError,
                                    "launch env_set must map strings to strings"):
            self.reserved(A.LaunchSpec(argv=["/bin/true"], cwd="/tmp",
                                       env_set={"A": 1}))

    def test_execute_raising_is_wrapped_in_a_launch_error(self):
        with self.assertRaisesRegex(SV.LaunchError,
                                    r"adapter\.execute failed: engine exploded"):
            self.reserved(RuntimeError("engine exploded"))

    def test_a_badly_labelled_limit_refuses_the_launch(self):
        # contracts reject a bad max_iterations at submit, so the reachable
        # defect is a preparation whose own labels are invalid: a monetary
        # limit labelled engine-enforced is advisory-only by policy.
        adapter = fake.FakeAdapter()
        real_prepare = adapter.prepare

        def prepare(request, ctx):
            p = real_prepare(request, ctx)
            p.limits = [{"name": "max_cost_usd", "value": 5, "enforced_by": "engine"}]
            return p
        adapter.prepare = prepare
        prev = A.register(adapter, replace=True)
        self.addCleanup(A.unregister, adapter.capability, prev)
        prep = self.approved("succeed")
        with self.assertRaisesRegex(SV.LaunchError,
                                    "monetary limits are advisory only"):
            SV.reserve(self.store, prep, env=self.env)
        # Nothing was reserved: the refusal consumed no approval.
        self.assertIsNone(self.store.get_invocation_for_prep(prep))

    def test_spawn_failure_marks_the_invocation_recovery_required(self):
        prep = self.approved("succeed")
        inv_id, _, _ = SV.reserve(self.store, prep, env=self.env, inv_id="inv-spawnfail")
        # An unspawnable supervisor binary: Popen raises OSError, and the
        # invocation must become recovery_required for `reconcile
        # --mark-not-launched`, not a traceback.
        with mock.patch.object(SV, "supervisor_argv",
                               return_value=["/nonexistent/supervisor-bin",
                                             "--db", str(self.db),
                                             "_supervise", inv_id]):
            with self.assertRaisesRegex(SV.LaunchError,
                                        "could not start the supervisor"):
                SV.spawn_supervisor(self.store, inv_id, env=self.env)
        inv = self.store.get_invocation(inv_id)
        self.assertEqual(inv["state"], SV.RECOVERY_REQUIRED)
        events = [e for e in self.store.list_events(entity_id=inv_id)
                  if e["event"] == f"state:{SV.RECOVERY_REQUIRED}"]
        self.assertIn("supervisor spawn failed", events[-1]["detail"]["reason"])
        self.assertEqual(self.starts(), [])

    def test_reap_skips_a_live_supervisor_and_reaps_it_once_exited(self):
        # A supervisor still running at reap time is skipped (TimeoutExpired
        # must not crash the reaper); a finished one is dropped from _SPAWNED.
        p = subprocess.Popen(["sleep", "30"])
        self.addCleanup(self.stop, p)
        SV._SPAWNED.append(p)
        SV.reap(timeout=0.2)   # live: skipped, kept for a later reap
        self.assertIn(p, SV._SPAWNED)
        p.kill()
        p.wait(timeout=10)
        SV.reap(timeout=1.0)
        self.assertNotIn(p, SV._SPAWNED)


if __name__ == "__main__":
    unittest.main()
