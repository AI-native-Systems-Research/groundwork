"""Results and evidence integration (plan §4.6, §5 R7; spec §11).

Two operations. Both are read-only over native engine output, both can be
retried, and neither ever launches, relaunches or retries an engine:

  collect(store, inv_id)  reconcile (exactly what `status` does: DB
                          transitions only), then the adapter's read-only
                          collect(), normalized. Appended to the DB as a
                          `collect:result` event.
  build(store, inv_id)    collect, then validate_answer.py (run as a
                          subprocess, unchanged), then the
                          ains.experiment-result/v1 message, checked by
                          contracts.py and appended as a `result:built`
                          event. export.py writes the latest one per
                          invocation to $STUDY/runtime/results/<inv-id>.json.

The record keeps four axes apart (spec §11):

  execution   lifecycle from the DB + the adapter's NativeState
              (completed | failed | cancelled | rejected | recovery_required).
              A negative or inconclusive finding is a completed execution.
  collection  not_run | collected | unavailable | failed, with a reason. A
              failed collection changes nothing else: the invocation row, its
              events and the native output stay exactly as they were.
  outcome     supported | not_supported | inconclusive | other | unavailable,
              with `classified_by: runtime | coordinator`. The runtime never
              guesses a hypothesis verdict. Nous records none at study level
              (plan §1 Q4), so it is `unavailable` with a reason unless the
              coordinator supplies one (`result --outcome C --explanation T`).
              That is accepted only for a completed, collected execution, and
              supported / not_supported / inconclusive only when the request
              stated a hypothesis. Spotlights static candidates_found and
              no_opportunity are `other` with an explanation (no hypothesis
              is tested). Anything not completed + collected is `unavailable`.
  validation  not_run | passed | failed, from validate_answer.py's exit code,
              on (in order) --answer, the study's final/answer.json, or the
              engine's own answer.json (Nous). Output kept as report_ref.

Findings appear only when the collection succeeded and the execution is
completed, or failed/cancelled with native evidence labelled partial
(decision D3: a Nous halt keeps its findings, labelled partial); then
findings.partial is true. Claims carry only the answer contract's five keys.
A Spotlights estimate (estimated impact, sort scores, proposals, cost) is
never a claim value: the spotlights-static normalizer re-derives the
estimate set from the native output, and any claim matching it fails the
collection (no findings). Candidates are listed without their estimates.

Normalizers turn one adapter's collect() output into Normalized. Lookup
order, so a later adapter (e.g. spotlights-telemetry, on hold under D10)
plugs in without changing this module:
  1. the adapter's own `normalize_collection(collected, record)` method,
  2. NORMALIZERS[capability] (register_normalizer()),
  3. generic(): the common keys only (state, reason, outcome, partial,
     claims, limitations, spend).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from . import RUNTIME_VERSION
from . import adapters as A
from . import limits as L
from .artifacts import make_ref
from .contracts import (CLAIM_KEYS, COLLECTION_STATES, HYPOTHESIS_OUTCOMES, OUTCOMES,
                        schema_id, validate_message)
from .envpolicy import find_secret_values
from .store import Store, StoreError, now

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))      # collect_spotlights, _collection, _config
VALIDATOR = SCRIPTS / "validate_answer.py"
VALIDATOR_NAME = "validate_answer.py"
VALIDATOR_TIMEOUT_S = 300
COORDINATOR_OUTCOMES = ("supported", "not_supported", "inconclusive", "other")
# The Spotlights native outcomes come from the fact the adapter reads
# (spotlights.json outcomes) — the same value adapters/spotlights_static.py
# emits, so a lock bump renaming one changes both sides together.
from .adapters.spotlights_static import (  # noqa: E402  (fact-sourced outcomes)
    CANDIDATES_FOUND as SPOT_CANDIDATES_FOUND, NO_OPPORTUNITY as SPOT_NO_OPPORTUNITY)
# Stored states a result can be built for (terminal, or stuck for the owner).
RESULTABLE = ("completed", "failed", "cancelled", "rejected", "not_launched",
              "recovery_required")
COLLECTABLE = ("completed", "failed", "cancelled")
EV_COLLECT, EV_RESULT = "collect:result", "result:built"


class ResultError(Exception):
    pass


@dataclass
class Normalized:
    """One adapter's collection in the shape results.py needs."""
    state: str                                  # COLLECTION_STATES
    reason: str | None = None
    outcome: str = "unavailable"                # the runtime's own view (OUTCOMES)
    explanation: str | None = None
    partial: bool = False
    claims: list[dict] = field(default_factory=list)       # five contract keys only
    hypotheses: list[dict] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    answer_path: str | None = None              # an engine-written answer.json
    answer_root: str | None = None              # its --root for the validator
    limitations: list[dict] = field(default_factory=list)  # {source, text, location?}
    methods: dict = field(default_factory=dict)
    spend: dict | None = None
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {"state": self.state, "reason": self.reason, "outcome": self.outcome,
                "explanation": self.explanation, "partial": self.partial,
                "claims": self.claims, "hypotheses": self.hypotheses,
                "candidates": self.candidates, "answer_path": self.answer_path,
                "limitations": self.limitations, "methods": self.methods,
                "spend": self.spend, "notes": self.notes}


Normalizer = Callable[[dict, dict], Normalized]
NORMALIZERS: dict[str, Normalizer] = {}


def register_normalizer(capability: str, fn: Normalizer) -> None:
    NORMALIZERS[capability] = fn


def contract_claims(claims) -> list[dict]:
    """Only the answer contract's five keys (drops verified, candidate_id, ...)."""
    return [{k: c.get(k) for k in CLAIM_KEYS} for c in claims or [] if isinstance(c, dict)]


def _spend(sp) -> dict | None:
    if not isinstance(sp, dict):
        return None
    out = {k: sp.get(k) for k in ("reported_usd", "completeness", "source")}
    if sp.get("note"):
        out["note"] = str(sp["note"])
    return out


# --- normalizers ---------------------------------------------------------------

def generic(collected: dict, record: dict) -> Normalized:
    st = collected.get("state")
    oc = collected.get("outcome") if isinstance(collected.get("outcome"), dict) else {}
    ok = st in COLLECTION_STATES
    return Normalized(
        state=st if ok else "failed",
        reason=collected.get("reason") or (None if ok else
                                           "adapter returned no collection state"),
        outcome=oc.get("classification") if oc.get("classification") in OUTCOMES
        else "unavailable",
        explanation=oc.get("reason") or oc.get("explanation")
        or "the adapter reported no outcome",
        partial=bool(collected.get("partial")),
        claims=contract_claims(collected.get("claims")),
        limitations=[x for x in collected.get("limitations") or [] if isinstance(x, dict)],
        spend=_spend(collected.get("spend")))


def nous(collected: dict, record: dict) -> Normalized:
    """adapters/nous.py collect(): {state: collected|unavailable, ...}."""
    if collected.get("state") != "collected":
        why = collected.get("reason") or "not a Nous v2 work dir"
        return Normalized("unavailable", why,
                          explanation=f"no native Nous output to classify: {why}")
    oc = collected.get("outcome") or {}
    hyps = []
    for h in collected.get("hypotheses") or []:
        e = {k: h.get(k) for k in ("id", "iteration", "classification", "native_status",
                                   "error_type", "statement_ref", "findings_ref")}
        e["partial"] = bool(h.get("partial"))
        if "partial_note" in h:
            e["partial_note"] = h.get("partial_note")
        hyps.append(e)
    notes = []
    if not collected.get("usable"):
        notes.append("collect.py marks this campaign unusable: "
                     + str((collected.get("bundle") or {}).get("reason")))
    return Normalized(
        "collected", outcome="unavailable",
        explanation=oc.get("reason") or "Nous records no study-level outcome",
        partial=bool(collected.get("partial")),
        claims=contract_claims(collected.get("claims")), hypotheses=hyps,
        answer_path=collected.get("answer_ref"), answer_root=collected.get("work_dir"),
        limitations=list(collected.get("limitations") or []),
        methods=dict(collected.get("methods") or {}),
        spend=_spend(collected.get("spend")), notes=notes)


def spotlights_estimates(record: dict) -> set:
    """Every Spotlights estimate value in the native output (collect_spotlights'
    own guard set), re-derived here so a claim can't slip past the adapter."""
    import collect_spotlights as CS
    out = Path((record.get("non_material") or {}).get("output_folder") or "")
    res = CS._load_json(out / "result.json") if (out / "result.json").is_file() else None
    if not isinstance(res, dict):
        return set()
    man_p = out / "run_manifest.json"
    man = CS._load_json(man_p) if man_p.is_file() else None
    sj = CS.find_sorted(out, "sorted_candidates.json")
    return CS.estimate_values(CS.report_of(res), CS._load_json(sj) if sj else None, man)


def spotlights_static(collected: dict, record: dict) -> Normalized:
    """adapters/spotlights_static.py collect(): {outcome, collection{state}, ...}."""
    col = collected.get("collection") or {}
    outcome, n = collected.get("outcome"), collected.get("n_candidates")
    sem = ((record.get("material") or {}).get("request") or {}).get("semantics") or {}
    methods = {"engine": "spotlights-engine", "include": sem.get("include"),
               "models": sem.get("models"), "hints": sem.get("hints"),
               "module_status": collected.get("module_status"), "n_candidates": n,
               "output_folder": (record.get("non_material") or {}).get("output_folder")}
    if outcome == SPOT_CANDIDATES_FOUND:
        expl = (f"{SPOT_CANDIDATES_FOUND}: {n} candidate(s). Candidates are model "
                "estimates of where to look, not measurements")
        if collected.get("partial"):
            expl += f" ({collected.get('reason')})"
    elif outcome == SPOT_NO_OPPORTUNITY:
        expl = (f"{SPOT_NO_OPPORTUNITY}: the run completed and found no candidate "
                "in the included modules")
    else:
        expl = f"{outcome}: {collected.get('reason') or 'no reason recorded'}"
    st = col.get("state") if col.get("state") in COLLECTION_STATES else "failed"
    reason = None
    if st != "collected":
        reason = col.get("error") or "; ".join(collected.get("notes") or []) \
            or "not collected"
    norm = Normalized(
        st, reason, explanation=expl, partial=bool(collected.get("partial")),
        outcome="other" if outcome in (SPOT_CANDIDATES_FOUND, SPOT_NO_OPPORTUNITY)
        else "unavailable",
        limitations=list(collected.get("limitations") or []), methods=methods,
        spend=_spend(collected.get("spend")), notes=list(collected.get("notes") or []))
    if st != "collected":
        return norm
    claims = contract_claims(collected.get("claims_draft"))
    forbidden = spotlights_estimates(record)
    bad = [c["id"] for c in claims if str(c.get("value")) in forbidden]
    bad += [v for v in (collected.get("estimate_guard") or {}).get("violations") or []
            if v not in bad]
    if bad:
        norm.state = "failed"
        norm.reason = "a Spotlights estimate became a claim value: " + ", ".join(
            map(str, bad))
        return norm
    norm.claims = claims
    norm.candidates = [{"id": c.get("id"), "rank": c.get("rank"),
                        "module": c.get("module"), "file": c.get("file"),
                        "cited": bool(c.get("cited"))}
                       for c in collected.get("candidates_top") or []
                       if isinstance(c, dict)]
    return norm


register_normalizer("nous", nous)
register_normalizer("spotlights-static", spotlights_static)


def normalize(adapter, collected: dict, record: dict) -> Normalized:
    fn = getattr(adapter, "normalize_collection", None)
    if callable(fn):
        return fn(collected, record)
    return NORMALIZERS.get(adapter.capability, generic)(collected, record)


# --- collect -------------------------------------------------------------------

def _setup(store: Store, inv_id: str, env, study):
    from .recovery import _adapter_view, _ctx, reconcile
    if store.get_invocation(inv_id) is None:
        raise ResultError(f"no invocation {inv_id!r}")
    reconcile(store, inv_id, env=env)          # DB transitions only; never launches
    inv = store.get_invocation(inv_id)
    rec, ctx = _ctx(store, inv, env)
    if study is None:
        dflt = Path(ctx.target) / "investigations" / Path(ctx.runs_dir).name
        study = dflt if dflt.is_dir() else None
    ctx.study = Path(study) if study is not None else None
    return inv, _adapter_view(inv), rec, ctx


def _merge_nous_bundle(raw: dict, study) -> str | None:
    """Nous: merge the collect.py bundle into <study>/collection.json["nous"],
    exactly as `collect.py --study` does (study content, not native output)."""
    if study is None or not Path(study).is_dir() or not raw.get("bundle"):
        return None
    import _collection
    return str(_collection.merge_section(Path(study), "nous", raw["bundle"]))


def _collect(store: Store, inv_id: str, env: dict, study) -> tuple[dict, Normalized]:
    inv, view, rec, ctx = _setup(store, inv_id, env, study)
    out = {"inv_id": inv_id, "capability": rec["capability"],
           "execution_state": inv["state"]}
    if inv["state"] not in COLLECTABLE:
        norm = Normalized("not_run", f"invocation is {inv['state']}: nothing to collect"
                          + ("" if inv["state"] in RESULTABLE else
                             " yet (run `status` until it is terminal)"))
        out.update(collection=norm.summary(), adapter_output=None)
        return out, norm
    raw = None
    try:
        adapter = A.get(rec["capability"])
        raw = adapter.collect(view, rec, ctx)
        if not isinstance(raw, dict):
            raise TypeError("adapter.collect returned no dict")
        norm = normalize(adapter, raw, rec)
        # The secret guard runs BEFORE the merge and covers the FULL raw
        # adapter output — the Nous bundle too, which never reaches
        # norm.summary() but does reach <study>/collection.json — so a hit
        # fails the collection with nothing merged and no output recorded.
        if find_secret_values(raw, env) or find_secret_values(norm.summary(), env):
            norm = Normalized("failed", "collection output contained a secret value; "
                              "not recorded")
            raw = None
        elif rec["capability"] == "nous" and norm.state == "collected":
            path = _merge_nous_bundle(raw, ctx.study)
            if path:
                norm.notes.append(f'merged the collect.py bundle into {path} under "nous"')
    except Exception as e:      # collection failure: record it, change nothing else
        norm = Normalized("failed", f"collection failed: {type(e).__name__}: {e}",
                          explanation="collection failed; the native output is kept as is")
        raw = None
    # Still checked after a collection failure: the failure reason embeds the
    # adapter's exception message, which may have echoed a secret value.
    if find_secret_values(norm.summary(), env):
        norm = Normalized("failed", "collection output contained a secret value; "
                          "not recorded")
        raw = None
    out.update(collection=norm.summary(), adapter_output=(
        {k: v for k, v in raw.items() if k != "bundle"} if raw else None))
    if find_secret_values(out["adapter_output"], env):
        out["adapter_output"] = None
    store.invocation_event(inv_id, EV_COLLECT, {"collection": norm.summary()}, env=env)
    return out, norm


def collect(store: Store, inv_id: str, *, env: Mapping[str, str] | None = None,
            study: str | os.PathLike | None = None) -> dict:
    """`runtime.py collect`: {inv_id, capability, execution_state, collection,
    adapter_output (minus the Nous bundle, which goes to collection.json)}."""
    return _collect(store, inv_id, dict(os.environ if env is None else env), study)[0]


# --- validation ----------------------------------------------------------------

def _results_dir(store: Store) -> Path:
    from .preparation import runtime_dir
    return runtime_dir(store) / "results"


def run_validator(store: Store, inv_id: str, answer: Path | None, *,
                  root: Path | None, target: Path, n: int) -> dict:
    """validate_answer.py (unchanged) as a subprocess. Never raises; a
    validator that can't run is `failed` with the reason, never `passed`."""
    if answer is None:
        return {"state": "not_run", "validator": VALIDATOR_NAME,
                "reason": "no answer.json to validate (none given with --answer, none "
                          "in the study's final/, none written by the engine)"}
    if not answer.is_file():
        return {"state": "not_run", "validator": VALIDATOR_NAME,
                "reason": f"answer file {answer} does not exist"}
    import _config
    # -B: no __pycache__ beside an install inside the target repo (D4).
    argv = [sys.executable, "-B", str(VALIDATOR), str(answer)]
    if root is not None:
        argv += ["--root", str(root)]
    # Citation checks only touch claims under citations/; harmless otherwise.
    argv += ["--check-citations", "--repo", f"{_config.repo_name(target)}={target}"]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                           timeout=VALIDATOR_TIMEOUT_S)
        rc, so, se = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        rc, so, se = None, "", f"timed out after {VALIDATOR_TIMEOUT_S}s"
    except OSError as e:
        rc, so, se = None, "", f"could not run the validator: {e}"
    d = _results_dir(store)
    d.mkdir(parents=True, exist_ok=True)
    rp = d / f"{inv_id}.validation-{n}.txt"
    rp.write_text(f"$ {VALIDATOR_NAME} {' '.join(argv[2:])}\nexit: {rc}\n"
                  f"--- stdout\n{so}--- stderr\n{se}", encoding="utf-8")
    first = next((ln for ln in se.splitlines() if ln and not ln.startswith("warning:")),
                 "")
    out = {"state": "passed" if rc == 0 else "failed", "validator": VALIDATOR_NAME,
           "exit_code": rc, "answer_ref": make_ref("answer-json", answer),
           "report_ref": make_ref("validator-report", rp)}
    if rc != 0:
        out["reason"] = (f"exit {rc}" if rc is not None else "did not finish") + (
            f": {first}" if first else "")
    return out


# --- the record ----------------------------------------------------------------

def _ref(kind: str, loc, *, final: bool = True) -> dict | None:
    """A file gets a digest when final; a directory or a mutable file is
    provisional (no portable identity, spec §6)."""
    if not loc:
        return None
    p = Path(loc)
    kind = re.sub(r"[^a-z0-9._-]+", "-", kind.lower()).strip("-.")[:64] or "artifact"
    try:
        if p.is_file():
            return make_ref(kind, p, provisional=not final)
        if p.exists():
            return make_ref(kind, p, provisional=True)
    except (OSError, ValueError):
        pass
    return None


def _execution(store: Store, inv: dict) -> dict:
    from .recovery import lifecycle
    terminal = inv["state"] in ("completed", "failed", "cancelled", "rejected",
                                "not_launched")
    native = inv.get("native") or {}
    started = next((e["at"] for e in store.list_events(entity_id=inv["inv_id"])
                    if e["event"] == "state:running"), None)
    diags, seen = [], set()
    logs = ((inv.get("launch") or {}).get("logs") or {})
    for name, loc in sorted(logs.items()):
        r = _ref(f"{name}-log", loc, final=terminal)
        if r:
            diags.append(r)
            seen.add(r["location"])
    for ev in native.get("evidence") or []:
        r = _ref(str(ev.get("kind", "artifact")), ev.get("location"))
        if r and r["location"] not in seen:
            diags.append(r)
            seen.add(r["location"])
    rc = inv.get("exit_code")
    if rc is None and terminal and inv.get("engine_pid"):
        rc = "unknown"
    return {"state": lifecycle(inv), "native_stop_reason": native.get("native_stop_reason"),
            "exit_code": rc, "started_at": started, "ended_at": inv.get("ended_at"),
            "diagnostics": diags}


def _limitations(norm: Normalized, inv: dict) -> list[dict]:
    native = inv.get("native") or {}
    out = []
    for x in norm.limitations:
        if isinstance(x, dict) and x.get("text"):
            e = {"source": str(x.get("source") or "adapter"), "text": str(x["text"])}
            r = _ref("limitation-source", x.get("location"))
            if r:
                e["artifact"] = r
            out.append(e)
    for w in native.get("warnings") or []:
        out.append({"source": "runtime:classify", "text": str(w)})
    if norm.partial and not any(e["source"] == "runtime" for e in out):
        out.append({"source": "runtime", "text": "findings are partial"
                    + (f": {native['reason']}" if native.get("reason") else "")})
    dedup, seen = [], set()
    for e in out:
        if (e["source"], e["text"]) not in seen:
            seen.add((e["source"], e["text"]))
            dedup.append(e)
    return dedup


def _limits(inv: dict, record: dict) -> list[dict]:
    t = L.elapsed_report(inv, record)
    observed = {b["name"]: t["elapsed_s"] for b in t["time_budgets"]}
    out = []
    for x in list(L.labelled(record)) + [dict(g) for g in L.RUNTIME_GUARDS]:
        e = {"name": str(x.get("name")), "value": x.get("value"),
             "enforced_by": x.get("enforced_by")}
        if x.get("note"):
            e["note"] = str(x["note"])
        if e["name"] in observed:
            e["observed"] = observed[e["name"]]
        out.append(e)
    return out


def _outcome(norm: Normalized, execution: dict, record: dict,
             coordinator: tuple[str, str] | None) -> dict:
    hyp = ((record.get("material") or {}).get("request") or {}).get("hypothesis")
    href = f"{record.get('request_message_id')}:body.hypothesis" if hyp else None
    partial = bool(norm.partial)
    if coordinator is not None:
        cls, expl = coordinator
        problems = []
        if cls not in COORDINATOR_OUTCOMES:
            problems.append(f"--outcome must be one of {', '.join(COORDINATOR_OUTCOMES)}")
        if not (isinstance(expl, str) and expl.strip()):
            problems.append("--explanation is required with --outcome")
        if execution["state"] != "completed":
            problems.append(f"execution is {execution['state']}: a coordinator outcome is "
                            "accepted only for a completed execution")
        if norm.state != "collected":
            problems.append(f"collection is {norm.state}: nothing collected to classify")
        if cls in HYPOTHESIS_OUTCOMES and not hyp:
            problems.append(f"{cls} needs a hypothesis stated in the request; this "
                            "request has none (use other)")
        if problems:
            raise ResultError("; ".join(problems))
        return {"classification": cls, "hypothesis_ref": href, "explanation": expl.strip(),
                "partial": partial, "classified_by": "coordinator"}
    if execution["state"] != "completed":
        expl = f"execution {execution['state']}"
        if execution.get("native_stop_reason"):
            expl += f" ({execution['native_stop_reason']})"
        if norm.state != "collected" and norm.reason:
            expl += f"; collection {norm.state}: {norm.reason}"
        elif norm.explanation:
            expl += f": {norm.explanation}"
        cls = "unavailable"
    elif norm.state != "collected":
        cls, expl = "unavailable", (f"collection {norm.state}: "
                                    f"{norm.reason or 'no reason recorded'}")
    else:
        cls = norm.outcome if norm.outcome in OUTCOMES else "unavailable"
        expl = norm.explanation or "no explanation recorded"
    return {"classification": cls,
            "hypothesis_ref": href if cls != "other" else None,
            "explanation": expl, "partial": partial, "classified_by": "runtime"}


def _findings(norm: Normalized, execution: dict) -> dict | None:
    if norm.state != "collected":
        return None
    st = execution["state"]
    if st != "completed" and not (st in ("failed", "cancelled") and norm.partial):
        return None
    f: dict[str, Any] = {"claims_from": "answer-contract/v1",
                         "partial": bool(norm.partial or st != "completed"),
                         "claims": norm.claims}
    if norm.hypotheses:
        f["hypotheses"] = norm.hypotheses
    if norm.candidates:
        f["candidates"] = norm.candidates
    r = _ref("answer-json", norm.answer_path)
    if r:
        f["answer_ref"] = r
    return f


def _pick_answer(answer, study, norm: Normalized) -> tuple[Path | None, Path | None]:
    if answer is not None:
        return Path(answer).resolve(), None
    if study is not None and (Path(study) / "final" / "answer.json").is_file():
        return (Path(study) / "final" / "answer.json").resolve(), None
    if norm.state == "collected" and norm.answer_path and Path(norm.answer_path).is_file():
        return Path(norm.answer_path), (Path(norm.answer_root) if norm.answer_root
                                        else None)
    return None, None


def build(store: Store, inv_id: str, *, env: Mapping[str, str] | None = None,
          study: str | os.PathLike | None = None, answer: str | os.PathLike | None = None,
          outcome: tuple[str, str] | None = None) -> dict:
    """`runtime.py result`: collect + validate + the ains.experiment-result/v1
    message. Returns {message, collection, notes}. Raises ResultError when
    refused (not terminal, bad --outcome); never reruns anything."""
    from .recovery import reconcile
    env = dict(os.environ if env is None else env)
    if store.get_invocation(inv_id) is None:
        raise ResultError(f"no invocation {inv_id!r}")
    reconcile(store, inv_id, env=env)
    inv = store.get_invocation(inv_id)
    if inv["state"] not in RESULTABLE:
        raise ResultError(f"invocation {inv_id} is {inv['state']}, not terminal; run "
                          "`status` until it is (a result never waits or reruns)")
    rec = store.get_preparation(inv["prep_id"])["record"]
    execution = _execution(store, inv)
    if outcome is not None:     # refuse a bad --outcome before collecting anything
        _outcome(Normalized("collected", explanation=""), execution, rec, outcome)
    col, norm = _collect(store, inv_id, env, study)
    inv = store.get_invocation(inv_id)
    execution = _execution(store, inv)
    oc = _outcome(norm, execution, rec, outcome)
    n = 1 + sum(1 for e in store.list_events(entity_id=inv_id) if e["event"] == EV_RESULT)
    if study is None:
        dflt = Path(rec["target_path"]) / "investigations" / store.path.parent.parent.name
        study = dflt if dflt.is_dir() else None
    ans, root = _pick_answer(answer, study, norm)
    validation = run_validator(store, inv_id, ans, root=root,
                               target=Path(rec["target_path"]), n=n)
    mat = rec.get("material") or {}
    body: dict[str, Any] = {
        "invocation_id": inv_id,
        "execution": execution,
        "collection": {"state": norm.state,
                       "reason": None if norm.state == "collected" else (
                           norm.reason or "no reason recorded")},
        "outcome": oc,
        "validation": validation,
        "methods": norm.methods,
        "limitations": _limitations(norm, inv),
        "limits": _limits(inv, rec),
        "spend": norm.spend or L.spend_unknown("not reported by the adapter"),
        "versions": {"runtime": RUNTIME_VERSION,
                     "adapter": f"{rec['capability']}/"
                                f"{(mat.get('adapter') or {}).get('version')}",
                     "engine_sha": (mat.get("versions") or {}).get("engine_sha")},
    }
    findings = _findings(norm, execution)
    if findings is not None:
        body["findings"] = findings
    tgt = mat.get("target") or {}
    msg = {"schema": schema_id("experiment-result"), "message_id": f"res-{inv_id}-{n}",
           "work_id": inv["work_id"], "revision": rec.get("revision", 1),
           "reply_to": rec.get("request_message_id"),
           "target": {"repo_name": tgt.get("repo_name"), "commit": tgt.get("commit")},
           "artifacts": [], "body": body}
    errs = validate_message(msg, env)
    if errs:
        raise StoreError("result record refused by contracts.py: " + "; ".join(errs))
    store.invocation_event(inv_id, EV_RESULT, {"message": msg, "built_at": now()}, env=env)
    return {"message": msg, "collection": col["collection"], "notes": norm.notes}


def latest_results(store: Store) -> dict[str, dict]:
    """{inv_id: {message, built_at}} for the latest result of each invocation."""
    out: dict[str, dict] = {}
    for i in store.list_invocations():
        for e in store.list_events(entity_id=i["inv_id"]):
            if e["event"] == EV_RESULT:
                out[i["inv_id"]] = e["detail"]
    return out
