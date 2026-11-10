"""Test-only `fake` adapter (plan R2). NOT shipped in the skill.

It registers under a real capability name (contracts only accept those),
"nous" by default, through adapters.register(..., replace=True); use()
returns a restore callable. Behaviour is driven by attributes so tests can
simulate drift:

  engine_sha        returned by versions(); change it to simulate an engine
                    upgrade between prepare and launch
  problems          extra validate() reasons
  MATERIAL_FIELDS   semantics keys "models", "experiment", "secrets"
  NON_MATERIAL      "run_id" (only names the native work dir)

prepare() renders `campaign.yaml` from the semantics (run_id inside it, so
the non-material templating is exercised), argv ["fake-engine", "run",
<prep_dir>/campaign.yaml, "--run-id", <run_id>] -- or, when
semantics.experiment names a fake engine in tests/fixtures/runtime/
fake_engines/ (succeed, fail, sleep_until_signalled, crash_partial,
ignore_sigterm, new_session_child), [sys.executable, <engine>.py, "run",
...] so R3 tests run a real (fake) process -- env_set
FAKE_PARENT=<runs_dir>, material var FAKE_ENGINE_MODE, file var
FAKE_MODELS_FILE, and secret vars from semantics.secrets.

The CLI entry point for subprocess tests is tests/_runtime_fake_cli.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "skills/investigation/scripts"
# tests/_runtime_install_smoke.py imports an INSTALLED skill's ains_runtime
# first; then this module must bind to that copy, never add the checkout's.
if "ains_runtime" not in sys.modules and str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ains_runtime import adapters as A  # noqa: E402
from ains_runtime.adapters.base import (Adapter, AdapterContext, LaunchSpec,  # noqa: E402
                                        NativeState, Prepared)

ENGINES = HERE / "fixtures" / "runtime" / "fake_engines"


def engine_argv(experiment) -> list[str]:
    if isinstance(experiment, str) and (ENGINES / f"{experiment}.py").is_file():
        return [sys.executable, str(ENGINES / f"{experiment}.py")]
    return ["fake-engine"]


class FakeAdapter(Adapter):
    version = "fake-0.1"
    MATERIAL_FIELDS = ("models", "experiment", "secrets")
    NON_MATERIAL_FIELDS = {"run_id": "names the native work dir only; the generated "
                                     "input differs only in the run_id line"}

    def __init__(self, capability: str = "nous", engine_sha: str = "e" * 40):
        self.capability = capability
        self.engine_sha = engine_sha
        self.problems: list[str] = []
        self.prepare_calls = 0
        self.cancel_grace = 1.0
        self.cancel_calls: list[dict] = []

    def validate(self, request, ctx: AdapterContext):
        sem = request["body"]["semantics"]
        out = list(self.problems)
        if not (isinstance(sem.get("models"), dict) and sem["models"].get("session")):
            out.append("semantics.models.session: required (no implicit default)")
        if not sem.get("run_id"):
            out.append("semantics.run_id: required")
        return out

    def versions(self, ctx):
        return {"engine_sha": self.engine_sha}

    def prepare(self, request, ctx: AdapterContext) -> Prepared:
        self.prepare_calls += 1
        body = request["body"]
        sem = body["semantics"]
        run_id = sem["run_id"]
        doc = {"run_id": run_id, "question": body["question"],
               "objective": body["objective"], "models": sem["models"],
               "experiment": sem.get("experiment"),
               "max_iterations": body["limits"].get("max_iterations")}
        text = "".join(f"{k}: {json.dumps(doc[k], sort_keys=True)}\n" for k in sorted(doc))
        campaign = str(ctx.prep_dir / "campaign.yaml")
        return Prepared(
            material={"engine": "fake-engine"},
            argv=[*engine_argv(sem.get("experiment")), "run", campaign, "--run-id", run_id],
            files={"campaign.yaml": text.encode("utf-8")},
            env_set={"FAKE_PARENT": str(ctx.runs_dir)},
            material_vars=("FAKE_ENGINE_MODE",),
            file_vars=("FAKE_MODELS_FILE",),
            secret_vars=tuple(sem.get("secrets") or ()),
            limits=[{"name": "max_iterations",
                     "value": body["limits"].get("max_iterations"),
                     "enforced_by": "engine"}],
            defaults=[{"field": "experiment", "value": sem.get("experiment") or "default",
                       "reason": "fake default"}],
            expected_artifacts=[{"kind": "fake-state",
                                 "location": str(ctx.runs_dir / run_id / "state.json")}],
            non_material={"run_id": run_id, "runs_dir": str(ctx.runs_dir)},
            native_run_id=run_id,
            summary=["fake adapter: test only"],
        )

    def execute(self, record, ctx) -> LaunchSpec:
        return LaunchSpec(argv=list(record["argv"]), cwd=record["cwd"],
                          env_set=dict(record["env_set"]),
                          native_run_id=record.get("native_run_id"))

    def collect(self, invocation, record, ctx):
        return {"state": "unavailable"}

    def cancel(self, invocation, record, ctx, *, interrupt=False):
        self.cancel_calls.append({"inv_id": invocation["inv_id"], "interrupt": interrupt})
        if interrupt:
            return self.interrupt_engine(invocation)
        return self.stop_process_tree(invocation, grace=self.cancel_grace)

    def work_dir(self, record) -> Path:
        return Path(record["env_set"]["FAKE_PARENT"]) / record["native_run_id"]

    def classify(self, invocation, record, ctx) -> NativeState:
        """Fake native rules: final.json decides; never the exit code alone."""
        w = self.work_dir(record)
        ev = [{"kind": "fake-" + p.stem, "location": str(p)}
              for p in sorted(w.glob("*")) if p.is_file()] if w.is_dir() else []
        partial = (w / "partial.txt").exists()
        cancel = invocation.get("cancel_requested_at") is not None
        rc = invocation.get("exit_code")
        try:
            status = json.loads((w / "final.json").read_text())["status"]
        except (OSError, ValueError, KeyError):
            status = None
        if status == "completed":
            return NativeState("completed", "completed", evidence=ev)
        if status == "stopped":
            if cancel:
                return NativeState("cancelled", "stopped", partial=True, evidence=ev)
            return NativeState("failed", "stopped", reason="stopped outside the runtime",
                               evidence=ev)
        if status == "failed":
            return NativeState("failed", "failed", reason="engine reported failure",
                               evidence=ev)
        if not ev:
            return NativeState("unknown", reason="no native output", evidence=ev)
        if cancel and (rc is None or rc < 0):
            return NativeState("cancelled", None, partial=True, evidence=ev,
                               reason=f"killed after a cancel request (exit {rc})")
        return NativeState("failed", None, partial=partial, evidence=ev,
                           reason=f"no final record (exit {rc if rc is not None else 'unknown'})")


def use(adapter: FakeAdapter | None = None):
    """Register a fake adapter; returns (adapter, restore)."""
    adapter = adapter or FakeAdapter()
    prev = A.register(adapter, replace=True)
    return adapter, (lambda: A.unregister(adapter.capability, prev))


def launch_worker(db, prep_id, env, barrier, queue):
    """multiprocessing target for the concurrent-launch test (spawn context):
    register the fake adapter, wait at the barrier, launch, report."""
    fake_adapter, _ = use()
    from ains_runtime import store as S
    from ains_runtime import supervisor as SV
    st = S.open_store(db, create=False)
    try:
        barrier.wait(timeout=30)
        try:
            res = SV.launch(st, prep_id, env=env)
            queue.put((prep_id, "ok", res.inv_id))
        except SV.LaunchError as e:
            queue.put((prep_id, "refused", str(e)))
        except Exception as e:  # report, don't hang the parent
            queue.put((prep_id, "error", repr(e)))
    finally:
        SV.reap(timeout=20)   # the succeed engine is quick; bounded either way
        st.close()
