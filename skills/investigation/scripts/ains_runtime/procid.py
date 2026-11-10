"""Process identity and process-tree helpers (plan §4.3, §4.4; R0 §1.7 items 4, 6).

A pid alone is never trusted (spec §8). A process's identity is the triple
pid + start time + argv:

  start time  an OPAQUE string tagged with how it was read, compared only
              with a string read the same way, never parsed as a date:
                proc-stat:<boot_id>:<ticks>   Linux, /proc/<pid>/stat field 22
                                              (clock ticks since boot; the
                                              boot id makes it unique across
                                              reboots). No `ps` needed.
                ps-lstart:<text>              elsewhere, `ps -o lstart=` run
                                              with LC_ALL=C and TZ=UTC forced
                                              in the CHILD environment (R0:
                                              the output changes with locale
                                              and TZ). Whitespace normalized.
              No usable method -> ProcIdError, which blocks launch (never
              "pid only").
  argv        the command line of a live process is compared with the argv
              the runtime spawned (argv_matches). This separates two
              processes that started in the same second with a reused pid
              (ps-lstart has 1-second resolution).

Zombies count as gone: a zombie has exited and only waits to be reaped.

Process-tree helpers for cancellation (§4.4): process_table() snapshots
(pid, ppid, pgid) for every live process (/proc on Linux, else
`ps -ax -o pid=,ppid=,pgid=,stat=`), descendants() walks it, and
signal_groups()/stop_process_groups() signal every DISTINCT process group
among an engine and its descendants. Signalling only the engine's own group
is not enough: R0 showed children started with start_new_session=True keep
running. This is best effort; a descendant reparented to init before the
snapshot is missed, so callers report survivors.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

PROC_ROOT = Path("/proc")
PS_TIMEOUT = 10
METHOD_PROC = "proc-stat"
METHOD_PS = "ps-lstart"


class ProcIdError(Exception):
    """Process start times can't be read on this host (launch is blocked)."""


@dataclass
class Identity:
    pid: int
    start: str            # tagged opaque start time
    cmdline: str          # argv joined by single spaces (as observed)
    zombie: bool = False


# --- method selection -------------------------------------------------------

def _ps_path() -> str | None:
    return shutil.which("ps")


def _proc_usable() -> bool:
    return sys.platform.startswith("linux") and (PROC_ROOT / "self" / "stat").is_file()


def method() -> str | None:
    """'proc-stat', 'ps-lstart', or None when neither works."""
    if _proc_usable():
        return METHOD_PROC
    if _ps_path() is not None:
        return METHOD_PS
    return None


def require() -> str:
    """The start-time method, or ProcIdError (launch-blocking, doctor reports it)."""
    m = method()
    if m is None:
        raise ProcIdError("cannot read process start times on this host: no "
                          f"{PROC_ROOT}/<pid>/stat and no `ps` on PATH; launch is "
                          "blocked (a pid alone is never trusted)")
    if m == METHOD_PS:
        # Prove it works for this very process before trusting it.
        if identity(os.getpid()) is None:
            raise ProcIdError("`ps -o lstart=` gave no start time for the current "
                              "process; launch is blocked")
    return m


# --- reading one process ----------------------------------------------------

def _ps_env() -> dict:
    env = dict(os.environ)
    env.update(LC_ALL="C", LANG="C", TZ="UTC")
    return env


def _run_ps(args: Sequence[str]) -> str | None:
    ps = _ps_path()
    if ps is None:
        return None
    try:
        r = subprocess.run([ps, *args], capture_output=True, text=True,
                           env=_ps_env(), timeout=PS_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout


_BOOT_ID: str | None = None


def _boot_id() -> str:
    global _BOOT_ID
    if _BOOT_ID is None:
        try:
            _BOOT_ID = (PROC_ROOT / "sys/kernel/random/boot_id").read_text().strip()
        except OSError:
            _BOOT_ID = "unknown-boot"
    return _BOOT_ID


def _proc_stat_fields(pid: int) -> list[str] | None:
    """Fields 3.. of /proc/<pid>/stat (after the parenthesized comm)."""
    try:
        raw = (PROC_ROOT / str(pid) / "stat").read_text()
    except OSError:
        return None
    rest = raw.rsplit(")", 1)
    if len(rest) != 2:
        return None
    return rest[1].split()


def _proc_cmdline(pid: int) -> str | None:
    try:
        raw = (PROC_ROOT / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    return " ".join(a.decode("utf-8", "replace") for a in raw.split(b"\0") if a)


def identity(pid: int, *, include_zombie: bool = False) -> Identity | None:
    """The identity of a live process, or None (no such process / zombie)."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    m = method()
    if m == METHOD_PROC:
        f = _proc_stat_fields(pid)
        if f is None or len(f) < 20:
            return None
        zombie = f[0] in ("Z", "X")
        ident = Identity(pid, f"{METHOD_PROC}:{_boot_id()}:{f[19]}",
                         _proc_cmdline(pid) or "", zombie)
    elif m == METHOD_PS:
        out = _run_ps(["-ww", "-o", "stat=", "-o", "lstart=", "-o", "command=",
                       "-p", str(pid)])
        if not out or not out.strip():
            return None
        toks = out.strip().splitlines()[0].split(None, 6)
        if len(toks) < 6:
            return None
        zombie = toks[0].startswith("Z")
        ident = Identity(pid, f"{METHOD_PS}:{' '.join(toks[1:6])}",
                         toks[6].strip() if len(toks) > 6 else "", zombie)
    else:
        return None
    if ident.zombie and not include_zombie:
        return None
    return ident


def start_time(pid: int, *, include_zombie: bool = False) -> str | None:
    ident = identity(pid, include_zombie=include_zombie)
    return ident.start if ident else None


def argv_matches(cmdline: str, argv: Sequence[str] | None) -> bool:
    """Does an observed command line belong to a process spawned with argv?

    The observed form may differ in argv[0] (an interpreter shown in place of
    a shebang script, a framework Python's rewritten path), so the arguments
    must match exactly as the tail and the program's basename must appear in
    the head. Single spaces join both sides.
    """
    if not argv or not cmdline:
        return False
    argv = [str(a) for a in argv]
    obs = " ".join(cmdline.split())
    prog = os.path.basename(argv[0])
    if len(argv) == 1:
        head = obs.split(" ", 1)[0]
        return os.path.basename(head) == prog
    tail = " ".join(" ".join(argv[1:]).split())
    if not obs.endswith(" " + tail):
        return False
    head = obs[: -len(tail) - 1]
    if prog in head:
        return True
    # Two Python launchers (python3 vs python3.11 vs a framework build's
    # "Python") are the same program for this purpose; the tail (script
    # path + per-invocation arguments) carries the identity.
    hb = os.path.basename(head.split(" ", 1)[0]).lower()
    return prog.lower().startswith("python") and hb.startswith("python")


def matches(pid: int | None, start: str | None, argv: Sequence[str] | None) -> bool:
    """True only if pid is live, its start time equals start, and argv matches."""
    if not pid or not start:
        return False
    ident = identity(int(pid))
    if ident is None or ident.start != start:
        return False
    if argv is None:
        return True
    return argv_matches(ident.cmdline, argv)


# --- process table, descendants, groups -------------------------------------

@dataclass
class Proc:
    pid: int
    ppid: int
    pgid: int


def process_table() -> list[Proc]:
    """(pid, ppid, pgid) of every live (non-zombie) process we can see."""
    out: list[Proc] = []
    if _proc_usable():
        for d in PROC_ROOT.iterdir():
            if not d.name.isdigit():
                continue
            f = _proc_stat_fields(int(d.name))
            if f is None or len(f) < 3 or f[0] in ("Z", "X"):
                continue
            out.append(Proc(int(d.name), int(f[1]), int(f[2])))
        return out
    text = _run_ps(["-ax", "-o", "pid=", "-o", "ppid=", "-o", "pgid=", "-o", "stat="])
    if text is None:
        raise ProcIdError("cannot list processes (no /proc and `ps -ax` failed)")
    for line in text.splitlines():
        t = line.split()
        if len(t) < 4 or t[3].startswith("Z"):
            continue
        try:
            out.append(Proc(int(t[0]), int(t[1]), int(t[2])))
        except ValueError:
            continue
    return out


def descendants(root: int, table: list[Proc] | None = None) -> list[Proc]:
    """Live descendants of root (by ppid), not including root."""
    table = process_table() if table is None else table
    kids: dict[int, list[Proc]] = {}
    for p in table:
        kids.setdefault(p.ppid, []).append(p)
    out, todo, seen = [], [root], {root}
    while todo:
        for c in kids.get(todo.pop(), []):
            if c.pid not in seen:
                seen.add(c.pid)
                out.append(c)
                todo.append(c.pid)
    return out


def _protected_groups() -> set[int]:
    prot = {0, 1}
    try:
        prot.add(os.getpgrp())
    except OSError:
        pass
    return prot


def tree_groups(root: int, *, root_pgid: int | None = None,
                table: list[Proc] | None = None) -> list[int]:
    """Distinct pgids among root (if live), its group, and its descendants."""
    table = process_table() if table is None else table
    groups = set()
    if root_pgid is not None:
        groups.add(root_pgid)
    for p in table:
        if p.pid == root:
            groups.add(p.pgid)
    for p in descendants(root, table):
        groups.add(p.pgid)
    # Members of the recorded groups may have been reparented: include their
    # descendants too.
    for p in table:
        if p.pgid in groups:
            groups.update(d.pgid for d in descendants(p.pid, table))
    return sorted(groups - _protected_groups())


def group_members(pgids: Iterable[int], table: list[Proc] | None = None) -> list[int]:
    pg = set(pgids)
    table = process_table() if table is None else table
    return sorted(p.pid for p in table if p.pgid in pg)


def signal_groups(pgids: Iterable[int], sig: int) -> list[dict]:
    """killpg each group; returns [{pgid, signal, result}]. Never raises."""
    out = []
    for g in sorted(set(pgids) - _protected_groups()):
        try:
            os.killpg(g, sig)
            res = "sent"
        except ProcessLookupError:
            res = "no such group"
        except PermissionError:
            res = "permission denied"
        out.append({"pgid": g, "signal": signal.Signals(sig).name, "result": res})
    return out


def stop_process_groups(engine_pid: int | None, engine_start: str | None,
                        engine_argv: Sequence[str] | None, *, grace: float = 10.0,
                        poll: float = 0.05) -> dict:
    """Best-effort tree stop for engines without a native stop (§4.4).

    Snapshot the engine's descendants, SIGTERM every distinct pgid, wait up
    to `grace` seconds for the groups to empty, re-snapshot, SIGKILL the
    same groups (plus any group found in the re-snapshot), and report
    survivors. The engine's own group (pgid == engine pid: the supervisor
    starts it in a new session) is signalled only when the engine identity
    still matches or no live process holds that pid, so a reused pid's group
    is never signalled.

    Returns {"mechanism": "process-groups", "requested": bool, "groups":
    [pgid], "signalled": [{pgid, signal, result}], "survivors": [pid],
    "error": str | None}. It never claims the engine stopped; the runtime
    confirms that from the supervisor's exit record and a fresh snapshot.
    """
    res = {"mechanism": "process-groups", "requested": False, "groups": [],
           "signalled": [], "survivors": [], "error": None}
    if not engine_pid:
        res["error"] = "no engine pid recorded"
        return res
    try:
        table = process_table()
        holder = identity(int(engine_pid))
        own_ok = holder is None or (engine_start is not None and
                                    matches(engine_pid, engine_start, engine_argv))
        if holder is not None and not own_ok:
            res["error"] = "engine pid now belongs to a different process; not signalled"
            return res
        root_pgid = int(engine_pid)
        groups = tree_groups(int(engine_pid), root_pgid=root_pgid, table=table)
        res["groups"] = groups
        res["signalled"] += signal_groups(groups, signal.SIGTERM)
        res["requested"] = True
        deadline = time.monotonic() + max(0.0, grace)
        while time.monotonic() < deadline and group_members(groups):
            time.sleep(poll)
        table = process_table()
        extra = set()
        for pid in group_members(groups, table):
            extra.update(d.pgid for d in descendants(pid, table))
        groups2 = sorted((set(groups) | extra) - _protected_groups())
        if group_members(groups2, table):
            res["signalled"] += signal_groups(groups2, signal.SIGKILL)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and group_members(groups2):
                time.sleep(poll)
        res["groups"] = groups2
        res["survivors"] = group_members(groups2)
    except ProcIdError as e:
        res["error"] = str(e)
    return res


def signal_if_match(pid: int | None, start: str | None, argv: Sequence[str] | None,
                    sig: int) -> bool:
    """Send sig to one process only if its identity matches (e.g. Nous SIGINT)."""
    if not matches(pid, start, argv):
        return False
    try:
        os.kill(int(pid), sig)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def find_by_argv(argv: Sequence[str] | None) -> list[int]:
    """Pids of live processes whose command line matches argv (recovery evidence)."""
    if not argv:
        return []
    found = []
    if _proc_usable():
        for d in PROC_ROOT.iterdir():
            if d.name.isdigit():
                f = _proc_stat_fields(int(d.name))
                if f is None or f[0] in ("Z", "X"):
                    continue
                if argv_matches(_proc_cmdline(int(d.name)) or "", argv):
                    found.append(int(d.name))
        return sorted(found)
    text = _run_ps(["-ax", "-ww", "-o", "pid=", "-o", "stat=", "-o", "command="])
    for line in (text or "").splitlines():
        t = line.split(None, 2)
        if len(t) == 3 and not t[1].startswith("Z") and argv_matches(t[2], argv):
            found.append(int(t[0]))
    return sorted(p for p in found if p != os.getpid())
