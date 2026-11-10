"""Capability adapter protocol (spec §4, §10; plan §3.1, §4.2).

An adapter translates one capability's portable request into a native
invocation and back. It never chooses the next capability, never retries on
its own, and never changes approved semantics (spec §4). The runtime owns
everything around it: the material record, digest, card, approval, the
revalidation before launch, and (R3) process control.

Lifecycle and who calls what:

  validate(request, ctx) -> list[str]
      Native requirements for this request: availability, required
      semantics present, collisions. [] = ok. Called by `prepare`, and again
      by preparation.revalidate() before launch. Must not write anything.

  prepare(request, ctx) -> Prepared
      The exact invocation: generated input files (as bytes; the runtime
      writes them under ctx.prep_dir and makes them read-only), the argv,
      env the runtime sets, the env/file names the adapter reads, labelled
      limits, inferred defaults, and the adapter's own material fields.
      Must not write anything itself.

  versions(ctx) -> dict
      The adapter-visible versions that are material (e.g. {"engine_sha":
      ...}). Compared again before launch: any drift blocks.

  execute(record, ctx) -> LaunchSpec
      Builds the spawn spec from the STORED preparation record (never from
      the request). R3's supervisor spawns it; the adapter does not.

  collect(invocation, record, ctx) -> dict
      Read-only collection of native output (R4-R7). Retryable.

  classify(invocation, record, ctx) -> NativeState            (R3 hook)
      Read-only lifecycle classification from NATIVE evidence (plan §1 Q2,
      Q4). The runtime calls it only once the engine process is known to be
      gone: the supervisor recorded its exit (invocation["exit_code"] is an
      int; negative = killed by that signal), or both supervisor and engine
      are gone (exit_code None = unknown). invocation["cancel_requested_at"]
      is non-null when the runtime recorded a cancel request. Return
      NativeState(state=completed|failed|cancelled|unknown, ...). The exit
      code alone must never make a run `completed`. `unknown` becomes
      `failed` (reason unclassified_native_state). A `cancelled` answer is
      accepted only if no process is left in the groups a cancel signalled.

  evidence(invocation, record, ctx) -> list[dict]             (R3 hook)
      Native artifacts that exist now ({kind, location}), used as recovery
      evidence ("did this launch happen?"). Default: the record's
      expected_artifacts that exist on disk. Read-only.

  cancel(invocation, record, ctx, *, interrupt=False) -> dict
      The capability's stop mechanism (plan §4.4). Called by `runtime.py
      cancel` (and by the supervisor if a cancel arrived while it was
      spawning), only while the invocation is not terminal. `invocation`
      is the DB row plus "engine_argv" (the argv the supervisor spawned).
      Native paths come from the record. Return
      {"mechanism": str, "requested": bool, "error": str | None, ...}; a
      process-group stop also returns "groups" and "survivors" (see
      stop_process_tree). Never claims the engine stopped: the runtime
      reports cancel_requested until classify() confirms a terminal state.

Material policy (plan §4.2): `MATERIAL_FIELDS` lists the request
`semantics` keys that are material; `NON_MATERIAL_FIELDS` maps each other
allowed key to the reason it may vary without changing the experiment (with
a test per adapter). Any other semantics key is rejected by
check_semantics(), so nothing is excluded from the digest silently.
Non-material values that end up inside argv or generated inputs (run ids,
output paths) are listed in Prepared.non_material; the runtime replaces them
with "{name}" placeholders before digesting.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass
class AdapterContext:
    """What the runtime gives an adapter. Paths are absolute, local."""
    target: Path                 # target repo toplevel
    runs_dir: Path               # $RUNS/<repo>/<slug>/ (native output goes here)
    prep_dir: Path               # .runtime/prepared/<prep-id>/ (may not exist yet)
    env: Mapping[str, str]       # the caller's environment (read-only)
    repo_name: str = ""
    work_id: str = ""


@dataclass
class Prepared:
    """An adapter's prepared invocation (see module docstring)."""
    material: dict                              # adapter material fields
    argv: list[str]                             # no shell; argv[0] the program
    files: dict[str, bytes] = field(default_factory=dict)   # relpath -> bytes
    cwd: str | None = None                      # default: the target toplevel
    env_set: dict[str, str] = field(default_factory=dict)   # runtime-set, material
    material_vars: tuple[str, ...] = ()         # inherited vars, value is material
    file_vars: tuple[str, ...] = ()             # inherited vars naming a file
    material_files: dict[str, str] = field(default_factory=dict)  # label -> path
    secret_vars: tuple[str, ...] = ()           # presence only
    limits: list[dict] = field(default_factory=list)   # {name, value, enforced_by}
    defaults: list[dict] = field(default_factory=list)  # {field, value, reason}
    expected_artifacts: list[dict] = field(default_factory=list)  # {kind, location}
    non_material: dict[str, str] = field(default_factory=dict)    # name -> value
    native_run_id: str | None = None
    summary: list[str] = field(default_factory=list)   # extra card lines


@dataclass
class LaunchSpec:
    argv: list[str]
    cwd: str
    env_set: dict[str, str]
    native_run_id: str | None = None


@dataclass
class NativeState:
    """An adapter's classification of a finished (or lost) engine run."""
    state: str                                  # completed|failed|cancelled|unknown
    native_stop_reason: str | None = None       # e.g. Nous ledger stop_reason
    reason: str | None = None                   # why (failed/unknown), plain text
    partial: bool = False                       # findings must be labelled partial
    evidence: list[dict] = field(default_factory=list)   # {kind, location}
    warnings: list[str] = field(default_factory=list)

    STATES = ("completed", "failed", "cancelled", "unknown")

    def as_dict(self) -> dict:
        return {"state": self.state, "native_stop_reason": self.native_stop_reason,
                "reason": self.reason, "partial": self.partial,
                "evidence": list(self.evidence), "warnings": list(self.warnings)}


class Adapter(abc.ABC):
    capability: str = ""
    version: str = ""
    MATERIAL_FIELDS: tuple[str, ...] = ()
    NON_MATERIAL_FIELDS: Mapping[str, str] = {}

    @abc.abstractmethod
    def validate(self, request: dict, ctx: AdapterContext) -> list[str]: ...

    @abc.abstractmethod
    def prepare(self, request: dict, ctx: AdapterContext) -> Prepared: ...

    @abc.abstractmethod
    def versions(self, ctx: AdapterContext) -> dict: ...

    @abc.abstractmethod
    def execute(self, record: dict, ctx: AdapterContext) -> LaunchSpec: ...

    @abc.abstractmethod
    def collect(self, invocation: dict, record: dict, ctx: AdapterContext) -> dict: ...

    @abc.abstractmethod
    def cancel(self, invocation: dict, record: dict, ctx: AdapterContext, *,
               interrupt: bool = False) -> dict: ...

    @abc.abstractmethod
    def classify(self, invocation: dict, record: dict,
                 ctx: AdapterContext) -> NativeState: ...

    def evidence(self, invocation: dict, record: dict, ctx: AdapterContext) -> list[dict]:
        out = []
        for a in record.get("expected_artifacts", []):
            loc = a.get("location")
            if loc and Path(loc).exists():
                out.append({"kind": a.get("kind", "artifact"), "location": loc})
        return out

    # --- stop helpers adapters may use in cancel() --------------------------

    @staticmethod
    def stop_process_tree(invocation: dict, *, grace: float = 10.0) -> dict:
        """Plan §4.4 Spotlights mechanism: SIGTERM every distinct process
        group among the engine and its descendants, SIGKILL the same groups
        after `grace` s, report survivors. Best effort; only signals the
        engine's group if its identity (pid + start + argv) still matches."""
        from .. import procid
        return procid.stop_process_groups(invocation.get("engine_pid"),
                                          invocation.get("engine_start"),
                                          invocation.get("engine_argv"), grace=grace)

    @staticmethod
    def interrupt_engine(invocation: dict) -> dict:
        """SIGINT the engine process only (Nous --interrupt: exit 130, resumable)."""
        import signal
        from .. import procid
        sent = procid.signal_if_match(invocation.get("engine_pid"),
                                      invocation.get("engine_start"),
                                      invocation.get("engine_argv"), signal.SIGINT)
        return {"mechanism": "sigint", "requested": sent,
                "error": None if sent else "engine identity does not match a live process"}

    # --- helpers ------------------------------------------------------------

    def check_semantics(self, request: dict) -> list[str]:
        """Reject semantics keys outside the declared material policy."""
        sem = request.get("body", {}).get("semantics", {})
        allowed = set(self.MATERIAL_FIELDS) | set(self.NON_MATERIAL_FIELDS)
        return [f"semantics.{k}: not in {self.capability} adapter's material policy"
                for k in sorted(set(sem) - allowed)]

    def material_semantics(self, request: dict) -> dict[str, Any]:
        """The material semantics fields (absent ones recorded as null)."""
        sem = request.get("body", {}).get("semantics", {})
        return {k: sem.get(k) for k in self.MATERIAL_FIELDS}
