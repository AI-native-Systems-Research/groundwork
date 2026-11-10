"""Reconciliation, status, cancellation and recovery (plan §4.3, §4.4; spec §8).

reconcile(store, inv_id) brings one invocation's recorded state in line
with the evidence. It is what `status` and `recover` run. It NEVER launches,
relaunches or retries anything; its only outputs are DB transitions.

  recorded            evidence                                   result
  ------------------  -----------------------------------------  -----------------
  reserved            launcher process (pid+start) alive, or a   unchanged (launch
                      supervisor process for this inv found      in progress)
                      by argv
  reserved/spawning   otherwise (launcher/supervisor gone)       recovery_required,
                                                                 with evidence: live
                                                                 engine-argv pids,
                                                                 native artifacts
  spawning            supervisor identity matches a live proc    unchanged
  running             engine identity (pid+start+argv) matches   unchanged
  running             engine gone, supervisor alive              unchanged (it will
                                                                 record the exit)
  running             engine gone, supervisor gone               adapter.classify,
                                                                 exit_code unknown
  exited              supervisor recorded the exit code          adapter.classify

A classification is terminal: completed | failed | cancelled. `unknown`
becomes `failed` (reason unclassified_native_state). While a cancel is
recorded and a process is still alive in any process group a cancel
signalled, the invocation stays non-terminal and is reported as
cancel_requested with the survivors listed.

Reported lifecycle (contracts.LIFECYCLE_STATES), from the stored state:
  reserved, spawning           -> accepted   (launch in progress)
  running, exited              -> running    (exited = classification pending)
  any of the above + a cancel  -> cancel_requested
  recovery_required            -> recovery_required
  completed/failed/cancelled   -> same
  rejected, not_launched       -> rejected   (not_launched: owner reconciled a
                                              recovery_required run as never
                                              launched; the approval stays used)
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Mapping

from . import adapters as A
from . import limits as L
from . import procid
from .canonical import canonical_json
from .envpolicy import find_secret_values
from .store import Store, StoreError, now
from .supervisor import (CANCELLED, COMPLETED, EXITED, FAILED, IN_FLIGHT, NOT_LAUNCHED,
                         RECOVERY_REQUIRED, REJECTED, RESERVED, RUNNING, SPAWNING,
                         TERMINAL, supervisor_argv)

CANCEL_SPAWN_WAIT_S = 5.0


class RecoveryError(Exception):
    pass


def lifecycle(inv: dict) -> str:
    s = inv["state"]
    if s in (COMPLETED, FAILED, CANCELLED, RECOVERY_REQUIRED):
        return s
    if s in (REJECTED, NOT_LAUNCHED):
        return "rejected"
    if inv.get("cancel_requested_at"):
        return "cancel_requested"
    return "accepted" if s in (RESERVED, SPAWNING) else "running"


def _ctx(store: Store, inv: dict, env: Mapping[str, str]):
    from .preparation import _context
    p = store.get_preparation(inv["prep_id"])
    rec = p["record"]
    work = store.get_work(inv["work_id"])
    ctx = _context(store, inv["prep_id"], Path(rec["target_path"]), env,
                   work["repo_name"] if work else "", inv["work_id"])
    return rec, ctx


def _adapter_view(inv: dict) -> dict:
    """The invocation as adapters see it: the DB row + the spawned argv."""
    return {**inv, "engine_argv": (inv.get("launch") or {}).get("argv")}


def _supervisor_alive(store: Store, inv: dict) -> bool:
    argv = supervisor_argv(store, inv["inv_id"])
    if inv.get("supervisor_pid"):
        return procid.matches(inv["supervisor_pid"], inv.get("supervisor_start"), argv)
    return bool(procid.find_by_argv(argv))


def _launch_in_progress(store: Store, inv: dict) -> bool:
    """reserved: the supervisor (once launch recorded it) or, before that,
    the launching process is still alive."""
    if inv.get("supervisor_pid"):
        return _supervisor_alive(store, inv)
    if procid.matches(inv.get("launcher_pid"), inv.get("launcher_start"), None):
        return True
    return bool(procid.find_by_argv(supervisor_argv(store, inv["inv_id"])))


def _engine_alive(inv: dict) -> bool:
    return procid.matches(inv.get("engine_pid"), inv.get("engine_start"),
                          (inv.get("launch") or {}).get("argv"))


def _last_cancel_groups(store: Store, inv_id: str) -> list[int]:
    groups: set[int] = set()
    for e in store.list_events(entity_id=inv_id):
        if e["event"] == "cancel:result":
            groups.update(int(g) for g in (e["detail"].get("result") or {}).get("groups", []))
    return sorted(groups)


def evidence(store: Store, inv: dict, env: Mapping[str, str]) -> dict:
    """Did this invocation launch? Live processes by argv + native artifacts."""
    out = {"engine_processes": [], "supervisor_processes": [], "artifacts": [],
           "errors": []}
    argv = (inv.get("launch") or {}).get("argv")
    try:
        out["engine_processes"] = procid.find_by_argv(argv)
        out["supervisor_processes"] = procid.find_by_argv(
            supervisor_argv(store, inv["inv_id"]))
    except procid.ProcIdError as e:
        out["errors"].append(str(e))
    try:
        rec, ctx = _ctx(store, inv, env)
        adapter = A.get(rec["capability"])
        out["artifacts"] = adapter.evidence(_adapter_view(inv), rec, ctx)
    except Exception as e:  # missing adapter etc.: evidence is incomplete, say so
        out["errors"].append(f"artifact evidence unavailable: {e}")
    return out


def _classify(store: Store, inv: dict, env: Mapping[str, str]) -> dict:
    rec, ctx = _ctx(store, inv, env)
    adapter = A.get(rec["capability"])
    ns = adapter.classify(_adapter_view(inv), rec, ctx)
    if not isinstance(ns, A.NativeState) or ns.state not in A.NativeState.STATES:
        raise RecoveryError("adapter.classify returned no valid NativeState")
    d = ns.as_dict()
    if ns.state == "unknown":
        d.update(state=FAILED, reason=ns.reason or "unclassified_native_state")
    if d["state"] == CANCELLED and not inv.get("cancel_requested_at"):
        d["warnings"].append("adapter reported cancelled but the runtime recorded no "
                             "cancel request")
    return d


def reconcile(store: Store, inv_id: str, *, env: Mapping[str, str] | None = None) -> dict:
    """Reconcile one invocation (module docstring). Returns status()'s view."""
    env = dict(os.environ if env is None else env)
    inv = store.get_invocation(inv_id)
    if inv is None:
        raise RecoveryError(f"no invocation {inv_id!r}")
    s = inv["state"]
    notes: list[str] = []
    if s in TERMINAL or s == RECOVERY_REQUIRED:
        return view(store, inv_id, env=env)

    if s in (RESERVED, SPAWNING):
        alive = (_launch_in_progress(store, inv) if s == RESERVED
                 else _supervisor_alive(store, inv))
        if not alive:
            # Re-read: the supervisor may have moved on while we looked.
            inv = store.get_invocation(inv_id)
            if inv["state"] == s:
                ev = evidence(store, inv, env)
                reason = ("the launcher/supervisor is gone and the engine's start was "
                          "never recorded; launch may or may not have happened")
                store.transition(inv_id, (s,), RECOVERY_REQUIRED,
                                 detail={"reason": reason, "evidence": ev,
                                         "relaunch": "never"})
        return view(store, inv_id, env=env)

    if s == RUNNING:
        if _engine_alive(inv) or _supervisor_alive(store, inv):
            return view(store, inv_id, env=env)
        inv = store.get_invocation(inv_id)
        if inv["state"] == RUNNING:
            notes.append("supervisor and engine are both gone; exit code unknown")
        elif inv["state"] != EXITED:
            return view(store, inv_id, env=env)

    # exited, or running with everything gone: classify from native evidence.
    try:
        native = _classify(store, inv, env)
    except Exception as e:
        return view(store, inv_id, env=env, notes=[f"classification pending: {e}"])
    if inv.get("cancel_requested_at"):
        groups = _last_cancel_groups(store, inv_id)
        try:
            survivors = procid.group_members(groups) if groups else []
        except procid.ProcIdError as e:
            survivors, notes = [], notes + [str(e)]
        if survivors:
            return view(store, inv_id, env=env, notes=notes + [
                f"cancel not confirmed: processes still alive in groups {groups}: "
                f"{survivors}"], survivors=survivors)
    native["warnings"] = native.get("warnings", []) + notes
    fields = {"native_json": canonical_json(native)}
    if inv.get("ended_at") is None:
        fields["ended_at"] = now()
    store.transition(inv_id, (EXITED, RUNNING), native["state"],
                     where={"engine_pid": inv.get("engine_pid")},
                     detail={"reason": native.get("reason"),
                             "native_stop_reason": native.get("native_stop_reason"),
                             "exit_code": inv.get("exit_code")},
                     **fields)
    return view(store, inv_id, env=env)


def view(store: Store, inv_id: str, *, env: Mapping[str, str] | None = None,
         notes: list[str] | None = None, survivors: list[int] | None = None) -> dict:
    inv = store.get_invocation(inv_id)
    p = store.get_preparation(inv["prep_id"])
    terminal = inv["state"] in TERMINAL
    out = {
        "inv_id": inv_id, "work_id": inv["work_id"], "prep_id": inv["prep_id"],
        "capability": inv["capability"], "lifecycle": lifecycle(inv),
        "state": inv["state"], "terminal": terminal,
        "exit_code": inv["exit_code"] if inv["exit_code"] is not None else (
            "unknown" if terminal and inv["engine_pid"] else None),
        "native": inv.get("native"),
        "cancel_requested_at": inv["cancel_requested_at"],
        "engine": {"pid": inv["engine_pid"], "start": inv["engine_start"]},
        "supervisor": {"pid": inv["supervisor_pid"], "start": inv["supervisor_start"]},
        "native_run_id": inv["native_run_id"],
        "created_at": inv["created_at"], "ended_at": inv["ended_at"],
        "logs": {**((inv.get("launch") or {}).get("logs") or {}),
                 "provisional": not terminal},
        "time": L.elapsed_report(inv, p["record"] if p else {}),
        "notes": list(notes or []),
    }
    if survivors:
        out["survivors"] = survivors
    if inv["state"] == RECOVERY_REQUIRED:
        ev = [e for e in store.list_events(entity_id=inv_id)
              if e["event"] == "state:recovery_required"]
        out["recovery"] = ev[-1]["detail"] if ev else {}
        out["notes"].append("the runtime will not relaunch this; the owner resolves it "
                            "(reconcile --mark-not-launched, or a new preparation)")
    return out


def status(store: Store, ident: str, *, env: Mapping[str, str] | None = None) -> dict:
    """status <work-id|inv-id>: reconcile, then report."""
    if store.get_invocation(ident) is not None:
        return reconcile(store, ident, env=env)
    w = store.get_work(ident)
    if w is None:
        raise RecoveryError(f"no work or invocation {ident!r}")
    invs = [reconcile(store, i["inv_id"], env=env) for i in store.list_invocations(ident)]
    return {"work_id": ident, "work_state": w["state"], "invocations": invs}


def recover(store: Store, *, env: Mapping[str, str] | None = None) -> dict:
    """Reconcile every in-flight invocation in this study's DB. Never relaunches."""
    out = []
    for i in store.list_invocations():
        if i["state"] in IN_FLIGHT:
            out.append(reconcile(store, i["inv_id"], env=env))
    return {"reconciled": out,
            "recovery_required": [i["inv_id"] for i in store.list_invocations()
                                  if i["state"] == RECOVERY_REQUIRED]}


def cancel(store: Store, inv_id: str, *, interrupt: bool = False,
           reason: str | None = None, env: Mapping[str, str] | None = None,
           spawn_wait_s: float = CANCEL_SPAWN_WAIT_S) -> dict:
    """Record a cancel request and call the adapter's stop mechanism (§4.4).

    The answer is never "stopped" on the strength of the request: the view's
    lifecycle is cancel_requested until reconcile confirms a terminal state.
    If the run is already terminal, its real state is returned unchanged.
    """
    env = dict(os.environ if env is None else env)
    first = reconcile(store, inv_id, env=env)
    if first["terminal"] or first["state"] == RECOVERY_REQUIRED:
        store.invocation_event(inv_id, "cancel:not_applicable",
                               {"state": first["state"], "reason": reason})
        first["cancel"] = {"requested": False,
                           "note": f"invocation is already {first['state']}; nothing "
                                   "to cancel"}
        return first
    store.request_cancel(inv_id, {"reason": reason, "interrupt": interrupt})
    inv = store.get_invocation(inv_id)
    if inv["state"] in (RESERVED, SPAWNING):
        # The supervisor checks the request before spawning; if it is past
        # that check, wait (bounded) for the engine identity, then stop it.
        deadline = time.monotonic() + spawn_wait_s
        while inv["state"] in (RESERVED, SPAWNING) and time.monotonic() < deadline:
            time.sleep(0.05)
            inv = store.get_invocation(inv_id)
    result: dict
    if inv["state"] == RUNNING:
        rec, ctx = _ctx(store, inv, env)
        try:
            result = A.get(rec["capability"]).cancel(_adapter_view(inv), rec, ctx,
                                                     interrupt=interrupt)
        except Exception as e:
            result = {"requested": False, "error": f"adapter cancel failed: {e}"}
        if not isinstance(result, dict):
            result = {"requested": False, "error": "adapter cancel returned no result"}
    elif inv["state"] in (RESERVED, SPAWNING):
        result = {"requested": False, "mechanism": "none",
                  "note": "engine not started yet; the supervisor will not start it "
                          "if it has not already"}
    else:
        result = {"requested": False, "mechanism": "none",
                  "note": f"engine already {inv['state']}; no signal sent"}
    leaks = find_secret_values(result, env)
    if leaks:
        result = {"requested": result.get("requested"), "error": "redacted: " +
                  "; ".join(leaks)}
    store.invocation_event(inv_id, "cancel:result", {"result": result})
    out = reconcile(store, inv_id, env=env)
    out["cancel"] = {**result, "receipt_only": True,
                     "confirmation": "a cancel request is not proof the engine "
                                     "stopped; `status` reports cancelled only once "
                                     "confirmed"}
    return out


def mark_not_launched(store: Store, inv_id: str, *, quote: str | None,
                      env: Mapping[str, str] | None = None) -> dict:
    """reconcile --mark-not-launched: the owner confirms a recovery_required
    invocation never launched. The approval stays consumed: any new attempt
    needs a new preparation and a new approval."""
    env = dict(os.environ if env is None else env)
    if not (isinstance(quote, str) and quote.strip()):
        raise RecoveryError("--quote is required: the owner's chat reply, verbatim")
    if find_secret_values({"quote": quote}, env):
        raise RecoveryError("--quote contains a secret value; refused")
    inv = store.get_invocation(inv_id)
    if inv is None:
        raise RecoveryError(f"no invocation {inv_id!r}")
    if inv["state"] != RECOVERY_REQUIRED:
        raise RecoveryError(f"{inv_id} is {inv['state']}, not recovery_required")
    ev = evidence(store, inv, env)
    if ev["engine_processes"] or ev["supervisor_processes"]:
        raise RecoveryError(
            f"refused: live processes match this invocation (engine "
            f"{ev['engine_processes']}, supervisor {ev['supervisor_processes']}); "
            "it did launch")
    if not store.transition(inv_id, (RECOVERY_REQUIRED,), NOT_LAUNCHED, ended_at=now(),
                            detail={"owner_quote": quote, "source": "chat",
                                    "evidence_at_reconcile": ev,
                                    "approval": "stays consumed"}):
        raise StoreError(f"{inv_id} changed state during reconcile")
    return view(store, inv_id, env=env)
