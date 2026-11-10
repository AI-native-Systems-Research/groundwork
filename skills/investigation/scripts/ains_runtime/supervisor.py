"""Launch and the detached supervisor (plan §4.3; spec §8).

launch(store, prep_id, ...) -- what `runtime.py launch` runs; returns at once:

  1. process identity must be readable on this host (procid.require), or
     the launch is blocked: a pid alone is never trusted
  2. preparation.revalidate() must pass (approval, versions, commit, clean
     tree, inputs, env), and limits.launch_problems() must be empty
  3. adapter.execute(record) -> LaunchSpec, from the STORED record
  4. store.reserve_invocation(): approval check + consumption + duplicate
     check + insert as `reserved`, in ONE transaction, before any process
     exists. The launch spec and the launcher's identity are stored with it.
  5. spawn `runtime.py --db <db> _supervise <inv-id>` detached
     (start_new_session=True), and return

supervise(store, inv_id) -- the private `_supervise` command:

  reserved -> spawning   record supervisor_pid + supervisor_start
             (a cancel recorded before this point -> `cancelled`, nothing spawned)
  spawning -> running    spawn the engine from the stored argv (a list, no
                         shell), in its own session, env built only from
                         the allowlist below; record engine_pid,
                         engine_start, native_run_id
  running  -> exited     wait; record exit_code (negative = killed by that
                         signal) and ended_at

It loads no adapter, classifies nothing, collects nothing, writes nothing
into engine directories, and never starts another invocation or capability:
there is no code path from here to a launch. Classification of `exited`
runs in `status` / `recover` (recovery.py), through the adapter.

Logs: .runtime/logs/<inv-id>.supervisor.log (the supervisor's own lines,
secret values redacted) and .runtime/logs/<inv-id>.engine.log (the engine's
stdout+stderr). Both are provisional until the invocation is terminal.

Engine environment: only the variables named in the launch spec's
`env_pass` (BASE_ENV_VARS, the preparation's material/file/secret variable
names, and ANTHROPIC_*/CLAUDE_* by prefix, D9), taken from the
supervisor's inherited environment, plus the adapter's `env_set`. Nothing
else is inherited; in particular no AINS_RUNTIME_TEST_ONLY_* variable.

TEST-ONLY CRASH INJECTION. crash_point(name) calls os._exit(CRASH_EXIT_CODE)
at a named point, so tests can simulate a crash between reserve and spawn
(and later). It fires only when BOTH variables are set exactly:

    AINS_RUNTIME_TEST_ONLY_CRASH_AT=<one of CRASH_POINTS>
    AINS_RUNTIME_TEST_ONLY_CRASH_ARM=kill-this-runtime-process-for-a-test

No skill instruction sets them, an unknown point name never fires, the
engine never inherits them, and `launch` prints a warning and records an
event whenever they are present, so an accidental arming is visible.
"""
from __future__ import annotations

import os
import secrets
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from . import adapters as A
from . import limits as L
from . import procid
from .canonical import canonical_json
from .envpolicy import OBSERVED_PREFIXES, redact
from .store import GrantError, Store, StoreError, now

RUNTIME_PY = Path(__file__).resolve().parent.parent / "runtime.py"
LOGS_DIR = "logs"

# Internal invocation states (stored). recovery.lifecycle() maps them to the
# reported lifecycle states of contracts.LIFECYCLE_STATES.
RESERVED, SPAWNING, RUNNING, EXITED = "reserved", "spawning", "running", "exited"
RECOVERY_REQUIRED, NOT_LAUNCHED = "recovery_required", "not_launched"
COMPLETED, FAILED, CANCELLED, REJECTED = "completed", "failed", "cancelled", "rejected"
TERMINAL = (COMPLETED, FAILED, CANCELLED, REJECTED, NOT_LAUNCHED)
IN_FLIGHT = (RESERVED, SPAWNING, RUNNING, EXITED)

BASE_ENV_VARS = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TEMP", "TMP",
                 "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ", "SSL_CERT_FILE",
                 "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
                 "HTTP_PROXY", "HTTPS_PROXY",
                 "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")

# --- test-only crash injection ----------------------------------------------

TEST_CRASH_AT_VAR = "AINS_RUNTIME_TEST_ONLY_CRASH_AT"
TEST_CRASH_ARM_VAR = "AINS_RUNTIME_TEST_ONLY_CRASH_ARM"
TEST_CRASH_ARM_VALUE = "kill-this-runtime-process-for-a-test"
CRASH_POINTS = ("launch:after_reserve", "supervisor:start", "supervisor:before_spawn",
                "supervisor:after_spawn", "supervisor:after_running",
                "supervisor:after_exit")
CRASH_EXIT_CODE = 86


def crash_hook_present(env: Mapping[str, str]) -> bool:
    return TEST_CRASH_AT_VAR in env or TEST_CRASH_ARM_VAR in env


def crash_point(name: str, env: Mapping[str, str] | None = None, log=None) -> None:
    """Test-only: os._exit here if (and only if) armed for this point."""
    assert name in CRASH_POINTS, name
    env = os.environ if env is None else env
    if env.get(TEST_CRASH_AT_VAR) != name:
        return
    if env.get(TEST_CRASH_ARM_VAR) != TEST_CRASH_ARM_VALUE:
        return
    if log is not None:
        log(f"TEST-ONLY crash injection fired at {name}; exiting {CRASH_EXIT_CODE}")
    os._exit(CRASH_EXIT_CODE)


# --- paths -------------------------------------------------------------------

def logs_dir(store: Store) -> Path:
    return store.path.parent / LOGS_DIR


def log_paths(store: Store, inv_id: str) -> dict:
    d = logs_dir(store)
    return {"supervisor": str(d / f"{inv_id}.supervisor.log"),
            "engine": str(d / f"{inv_id}.engine.log")}


def supervisor_argv(store: Store, inv_id: str) -> list[str]:
    """The exact argv launch spawns (and recovery looks for)."""
    return [sys.executable, str(RUNTIME_PY), "--db", str(store.path.resolve()),
            "_supervise", inv_id]


def new_inv_id() -> str:
    return "inv-" + secrets.token_hex(6)


# --- launch ------------------------------------------------------------------

class LaunchError(Exception):
    def __init__(self, reasons: list[str] | str):
        self.reasons = [reasons] if isinstance(reasons, str) else list(reasons)
        super().__init__("; ".join(self.reasons))


@dataclass
class LaunchResult:
    inv_id: str
    prep_id: str
    state: str
    supervisor_pid: int | None
    logs: dict
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"inv_id": self.inv_id, "prep_id": self.prep_id, "state": self.state,
                "supervisor_pid": self.supervisor_pid, "logs": self.logs,
                "warnings": self.warnings,
                "note": "launched under the runtime supervisor; the engine is not "
                        "confirmed running until `status` says so"}


# Popen handles of supervisors this process spawned (kept so they are reaped
# quietly when this is a long-lived process, e.g. a test runner).
_SPAWNED: list[subprocess.Popen] = []


def env_pass_names(record: dict, env: Mapping[str, str]) -> list[str]:
    pol = record.get("env_policy", {})
    names = set(BASE_ENV_VARS)
    for k in ("material_vars", "file_vars", "secret_vars"):
        names.update(pol.get(k, ()))
    names.update(n for n in env if n.startswith(tuple(OBSERVED_PREFIXES)))
    return sorted(n for n in names if not n.startswith("AINS_RUNTIME_TEST_ONLY_"))


def _check_spec(spec) -> list[str]:
    out = []
    if not isinstance(spec, A.LaunchSpec):
        return ["adapter.execute did not return a LaunchSpec"]
    if not (isinstance(spec.argv, list) and spec.argv
            and all(isinstance(a, str) and a for a in spec.argv)):
        out.append("launch argv must be a non-empty list of strings (no shell)")
    if not (isinstance(spec.cwd, str) and os.path.isabs(spec.cwd)):
        out.append("launch cwd must be an absolute path")
    if not (isinstance(spec.env_set, dict) and
            all(isinstance(k, str) and isinstance(v, str) for k, v in spec.env_set.items())):
        out.append("launch env_set must map strings to strings")
    return out


def reserve(store: Store, prep_id: str, *, env: Mapping[str, str] | None = None,
            inv_id: str | None = None) -> tuple[str, dict, list[str]]:
    """Launch steps 1-4 (module docstring): checks, then the one-transaction
    reservation. Returns (inv_id, launch_spec, warnings). Spawns nothing."""
    from .preparation import _context, revalidate
    env = dict(os.environ if env is None else env)
    inv_id = inv_id or new_inv_id()
    warnings: list[str] = []
    if crash_hook_present(env):
        warnings.append(f"TEST-ONLY crash-injection variables are set "
                        f"({TEST_CRASH_AT_VAR}/{TEST_CRASH_ARM_VAR}); unset them "
                        "outside the runtime's own tests")
    try:
        procid.require()
    except procid.ProcIdError as e:
        raise LaunchError(str(e)) from e

    rv = revalidate(store, prep_id, env=env)
    if not rv.ok:
        raise LaunchError(rv.problems)
    warnings += rv.warnings
    p = store.get_preparation(prep_id)
    rec = p["record"]
    lp = L.launch_problems(rec)
    if lp:
        raise LaunchError(lp)
    adapter = A.get(rec["capability"])
    work = store.get_work(p["work_id"])
    ctx = _context(store, prep_id, Path(rec["target_path"]), env,
                   work["repo_name"] if work else "", p["work_id"])
    try:
        spec = adapter.execute(rec, ctx)
    except Exception as e:
        raise LaunchError(f"adapter.execute failed: {e}") from e
    bad = _check_spec(spec)
    if bad:
        raise LaunchError(bad)
    launch_spec = {"argv": list(spec.argv), "cwd": spec.cwd, "env_set": dict(spec.env_set),
                   "native_run_id": spec.native_run_id,
                   "env_pass": env_pass_names(rec, env),
                   "logs": log_paths(store, inv_id)}

    me = os.getpid()
    try:
        store.reserve_invocation(inv_id, prep_id, expected_digest=rv.material_digest,
                                 repeat_of=rec.get("repeat_of"),
                                 launcher=(me, procid.start_time(me)),
                                 launch=launch_spec)
    except GrantError as e:
        raise LaunchError(str(e)) from e
    if crash_hook_present(env):
        store.invocation_event(inv_id, "test_crash_hook_present",
                               {"at": env.get(TEST_CRASH_AT_VAR)})
    return inv_id, launch_spec, warnings


def spawn_supervisor(store: Store, inv_id: str, *,
                     env: Mapping[str, str] | None = None) -> int:
    """Launch step 5: start the detached supervisor; returns its pid."""
    from .preparation import runtime_dir
    env = dict(os.environ if env is None else env)
    spec = store.get_invocation(inv_id)["launch"]
    logs_dir(store).mkdir(parents=True, exist_ok=True)
    sup_log = open(spec["logs"]["supervisor"], "ab")
    try:
        proc = subprocess.Popen(
            supervisor_argv(store, inv_id),
            cwd=str(runtime_dir(store)), env=env, stdin=subprocess.DEVNULL,
            stdout=sup_log, stderr=subprocess.STDOUT, start_new_session=True,
            close_fds=True)
    except OSError as e:
        # Nothing was spawned: the reservation stays (approval consumed); the
        # owner resolves it with `reconcile --mark-not-launched`.
        store.transition(inv_id, (RESERVED,), RECOVERY_REQUIRED,
                         detail={"reason": f"supervisor spawn failed: {e}"})
        raise LaunchError(f"could not start the supervisor: {e}; the invocation "
                          f"{inv_id} is recovery_required") from e
    finally:
        sup_log.close()
    _SPAWNED.append(proc)
    sident = procid.identity(proc.pid, include_zombie=True)
    store.record_if_null(inv_id, "launch:supervisor_spawned", supervisor_pid=proc.pid,
                         supervisor_start=sident.start if sident else None)
    return proc.pid


def launch(store: Store, prep_id: str, *, env: Mapping[str, str] | None = None,
           inv_id: str | None = None) -> LaunchResult:
    """Revalidate, reserve atomically, spawn the supervisor, return. See module doc."""
    env = dict(os.environ if env is None else env)
    reap()
    inv_id, spec, warnings = reserve(store, prep_id, env=env, inv_id=inv_id)
    crash_point("launch:after_reserve", env)
    pid = spawn_supervisor(store, inv_id, env=env)
    return LaunchResult(inv_id, prep_id, store.get_invocation(inv_id)["state"], pid,
                        spec["logs"], warnings)


# --- the supervisor ------------------------------------------------------------

class _Log:
    def __init__(self, env: Mapping[str, str]):
        self.env = env

    def __call__(self, line: str) -> None:
        sys.stdout.write(f"{now()} {redact(line, self.env)}\n")
        sys.stdout.flush()


def engine_env(spec: dict, env: Mapping[str, str]) -> dict:
    out = {n: env[n] for n in spec.get("env_pass", ()) if n in env}
    out.update(spec.get("env_set", {}))
    return out


def supervise(store: Store, inv_id: str, *, env: Mapping[str, str] | None = None) -> int:
    """Run one invocation's engine to exit. Returns the supervisor's exit code."""
    env = os.environ if env is None else env
    log = _Log(env)
    for s in (signal.SIGINT, signal.SIGHUP):
        signal.signal(s, signal.SIG_IGN)
    crash_point("supervisor:start", env, log)
    inv = store.get_invocation(inv_id)
    if inv is None or not inv.get("launch"):
        log(f"no reserved invocation {inv_id!r} with a launch spec; nothing to do")
        return 2
    spec = inv["launch"]
    me = os.getpid()
    mine = {"supervisor_pid": me, "supervisor_start": procid.start_time(me)}
    if inv["supervisor_pid"] not in (None, me):
        log(f"{inv_id} belongs to supervisor {inv['supervisor_pid']}; not spawning")
        return 1
    if not store.transition(inv_id, (RESERVED,), SPAWNING, **mine):
        log(f"{inv_id} is {store.get_invocation(inv_id)['state']}, not reserved; "
            "not spawning")
        return 1
    log(f"supervisor {me} for {inv_id}: spawning {spec['argv'][0]}")
    if store.get_invocation(inv_id)["cancel_requested_at"]:
        store.transition(inv_id, (SPAWNING,), CANCELLED, ended_at=now(),
                         detail={"reason": "cancel requested before the engine was "
                                           "spawned; nothing ran"})
        log("cancel requested before spawn; engine not started")
        return 0
    crash_point("supervisor:before_spawn", env, log)

    eng_log = open(spec["logs"]["engine"], "ab")
    try:
        proc = subprocess.Popen(spec["argv"], cwd=spec["cwd"],
                                env=engine_env(spec, env), stdin=subprocess.DEVNULL,
                                stdout=eng_log, stderr=subprocess.STDOUT,
                                start_new_session=True, close_fds=True)
    except OSError as e:
        store.transition(inv_id, (SPAWNING,), FAILED, ended_at=now(),
                         native_json=canonical_json({
                             "state": FAILED, "reason": f"engine spawn failed: {e}",
                             "native_stop_reason": None, "partial": False,
                             "evidence": [], "warnings": []}),
                         detail={"reason": "engine spawn failed"})
        log(f"engine spawn failed: {e}")
        return 1
    finally:
        eng_log.close()
    crash_point("supervisor:after_spawn", env, log)
    # The engine is our child, so it can't be reaped before wait(): read its
    # identity even if it already exited (zombie).
    ident = procid.identity(proc.pid, include_zombie=True)
    if not store.transition(inv_id, (SPAWNING,), RUNNING, engine_pid=proc.pid,
                            engine_start=ident.start if ident else None,
                            native_run_id=spec.get("native_run_id")):
        log(f"{inv_id} left `spawning` while the engine started; recording the pid "
            "as an event only")
        store.invocation_event(inv_id, "engine_spawned_out_of_band",
                               {"engine_pid": proc.pid})
    log(f"engine pid {proc.pid} running")
    crash_point("supervisor:after_running", env, log)
    rc = proc.wait()
    crash_point("supervisor:after_exit", env, log)
    ok = store.transition(inv_id, (RUNNING,), EXITED, exit_code=rc, ended_at=now(),
                          where={"engine_pid": proc.pid})
    log(f"engine exited {rc}" + ("" if ok else " (state already changed; not recorded)"))
    # Done. No collection, no classification, no next step (plan §4.3 step 6).
    return 0


def reap(timeout: float = 0.0) -> None:
    """Reap supervisors this process spawned that have exited (waits up to
    `timeout` s for each; 0 = no blocking)."""
    for p in list(_SPAWNED):
        try:
            p.wait(timeout=timeout) if timeout else p.poll()
        except subprocess.TimeoutExpired:
            pass
        if p.returncode is not None:
            _SPAWNED.remove(p)


def main_supervise(db: Path, inv_id: str) -> int:
    from .store import open_store
    try:
        store = open_store(db, create=False)
    except StoreError as e:
        print(f"_supervise: {e}", file=sys.stderr)
        return 2
    try:
        return supervise(store, inv_id)
    finally:
        store.close()
