"""Message contracts (plan §4.1, §4.6; spec §6, §11): stdlib validators.

Every message is an envelope:

  schema      "ains.<type>/v<N>"; <type> in SCHEMA_VERSIONS, <N> supported
  message_id  token (ID_RE); unique per payload (store.py enforces)
  work_id     token (ID_RE)
  revision    int >= 1
  reply_to    optional message_id token
  target      {repo_name: non-empty string, commit: 40- or 64-hex lowercase}
  artifacts   list of artifact refs (artifacts.py)
  body        type-specific object (validators below)

Validation is strict: unknown fields are rejected, required fields are never
filled in by inference, and any string containing the value of a
secret-named environment variable is refused (envpolicy.py). The JSON Schema
documents under references/runtime-schemas/ describe the same rules for
humans; this module is the authority.

Entry points:
  parse_schema(s)             -> (type, version) or raise ContractError
  validate_message(msg, env=) -> list of error strings ([] = valid)
  check_message(msg, env=)    -> msg, or raise ContractError(errors)
  SUBMITTABLE_TYPES           -> types an agent may `submit` (the rest are
                                 produced by the runtime)
"""
from __future__ import annotations

import re
from typing import Any, Callable, Mapping

from . import SCHEMA_VERSIONS
from .artifacts import validate_artifact_ref
from .envpolicy import find_secret_values

SCHEMA_RE = re.compile(r"^ains\.([a-z][a-z-]*)/v([0-9]+)$")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")

ENVELOPE_REQUIRED = ("schema", "message_id", "work_id", "revision", "target",
                     "artifacts", "body")
ENVELOPE_OPTIONAL = ("reply_to",)

CAPABILITIES = ("nous", "spotlights-static", "spotlights-telemetry",
                "telemetry-capture")
TASK_TYPES = ("debug", "verify", "optimize", "policy", "explain", "other")

# Lifecycle states reported to the coordinator (spec §8, plan §4.3/§4.4).
LIFECYCLE_STATES = ("accepted", "running", "waiting_for_answer", "completed",
                    "rejected", "failed", "cancelled", "cancel_requested",
                    "recovery_required")
TERMINAL_STATES = ("completed", "rejected", "failed", "cancelled")
RECEIPT_STATUSES = ("accepted", "rejected", "conflict")
OUTCOMES = ("supported", "not_supported", "inconclusive", "other", "unavailable")
VALIDATION_STATES = ("not_run", "passed", "failed")
COLLECTION_STATES = ("not_run", "collected", "unavailable", "failed")
HYPOTHESIS_OUTCOMES = ("supported", "not_supported", "inconclusive")
CLAIM_KEYS = ("id", "metric", "value", "condition", "evidence")   # answer contract
ENFORCED_BY = ("runtime", "engine", "advisory")
SPEND_COMPLETENESS = ("complete", "partial", "unknown")
NEXT_STEP_FIELDS = ("interpretation_of_prior", "remaining_uncertainty",
                    "expected_information_gain", "rationale")

# Types an agent may submit. receipt/progress/experiment-result are written by
# the runtime itself; a submitted one would be an agent-forged record.
SUBMITTABLE_TYPES = ("experiment-request", "clarification", "cancel")


class ContractError(ValueError):
    def __init__(self, errors: list[str] | str):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("; ".join(self.errors))


# --- small field checkers ---------------------------------------------------

class _V:
    """Collects errors for one object."""

    def __init__(self, obj: Any, path: str, errs: list[str]):
        self.obj, self.path, self.errs = obj, path, errs
        self.ok = isinstance(obj, dict)
        if not self.ok:
            errs.append(f"{path}: must be an object")

    def keys(self, required: tuple, optional: tuple = ()) -> None:
        if not self.ok:
            return
        for k in required:
            if k not in self.obj:
                self.errs.append(f"{self.path}.{k}: required")
        for k in sorted(set(self.obj) - set(required) - set(optional)):
            self.errs.append(f"{self.path}.{k}: unknown field")

    def _get(self, k):
        return self.obj.get(k) if self.ok else None

    def _has(self, k):
        return self.ok and k in self.obj

    def text(self, k, *, required=True, nullable=False):
        if not self._has(k):
            return  # missing-required reported by keys()
        v = self.obj[k]
        if v is None and nullable:
            return
        if not isinstance(v, str) or not v.strip():
            self.errs.append(f"{self.path}.{k}: must be a non-empty string")

    def token(self, k, regex=ID_RE):
        if self._has(k):
            v = self.obj[k]
            if not isinstance(v, str) or not regex.match(v):
                self.errs.append(f"{self.path}.{k}: must match {regex.pattern}")

    def enum(self, k, allowed, *, nullable=False):
        if self._has(k):
            v = self.obj[k]
            if v is None and nullable:
                return
            if v not in allowed:
                self.errs.append(f"{self.path}.{k}: must be one of {', '.join(allowed)}")

    def boolean(self, k):
        if self._has(k) and not isinstance(self.obj[k], bool):
            self.errs.append(f"{self.path}.{k}: must be a boolean")

    def integer(self, k, *, minimum=None, nullable=False):
        if self._has(k):
            v = self.obj[k]
            if v is None and nullable:
                return
            if not isinstance(v, int) or isinstance(v, bool) or (
                    minimum is not None and v < minimum):
                lo = f" >= {minimum}" if minimum is not None else ""
                self.errs.append(f"{self.path}.{k}: must be an integer{lo}")

    def number(self, k, *, nullable=False):
        if self._has(k):
            v = self.obj[k]
            if v is None and nullable:
                return
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                self.errs.append(f"{self.path}.{k}: must be a number")

    def mapping(self, k):
        if self._has(k) and not isinstance(self.obj[k], dict):
            self.errs.append(f"{self.path}.{k}: must be an object")

    def timestamp(self, k, *, nullable=False):
        if self._has(k):
            v = self.obj[k]
            if v is None and nullable:
                return
            if not isinstance(v, str) or not TIMESTAMP_RE.match(v):
                self.errs.append(f"{self.path}.{k}: must be an RFC 3339 timestamp")

    def array(self, k, item: Callable[[Any, str, list], None] | None = None, *,
              nonempty=False):
        if not self._has(k):
            return
        v = self.obj[k]
        if not isinstance(v, list):
            self.errs.append(f"{self.path}.{k}: must be an array")
            return
        if nonempty and not v:
            self.errs.append(f"{self.path}.{k}: must not be empty")
        if item:
            for i, x in enumerate(v):
                item(x, f"{self.path}.{k}[{i}]", self.errs)


def _ref_item(x, path, errs):
    errs.extend(validate_artifact_ref(x, path))


def _text_item(x, path, errs):
    if not isinstance(x, str) or not x.strip():
        errs.append(f"{path}: must be a non-empty string")


# --- schema id --------------------------------------------------------------

def parse_schema(schema: Any) -> tuple[str, int]:
    if not isinstance(schema, str):
        raise ContractError("schema: must be a string like ains.<type>/v<N>")
    m = SCHEMA_RE.match(schema)
    if not m:
        raise ContractError(f"schema: {schema!r} is not of the form ains.<type>/v<N>")
    mtype, version = m.group(1), int(m.group(2))
    if mtype not in SCHEMA_VERSIONS:
        raise ContractError(f"schema: unknown message type {mtype!r}")
    if version not in SCHEMA_VERSIONS[mtype]:
        supported = ", ".join(f"v{v}" for v in SCHEMA_VERSIONS[mtype])
        raise ContractError(
            f"schema: unsupported version {schema!r} (supported: {supported})")
    return mtype, version


def schema_id(mtype: str, version: int = 1) -> str:
    return f"ains.{mtype}/v{version}"


# --- bodies -----------------------------------------------------------------

def _experiment_request(body, path, errs, env_msg):
    v = _V(body, path, errs)
    v.keys(("capability", "question", "task_type", "objective", "locks",
            "limits", "semantics"),
           ("baseline", "hypothesis", "next_step", "repeat_of", "repeat_reason"))
    if not v.ok:
        return
    v.enum("capability", CAPABILITIES)
    v.text("question")
    v.enum("task_type", TASK_TYPES)
    v.text("objective")
    v.text("baseline", nullable=True)
    v.text("hypothesis", nullable=True)
    v.mapping("locks")
    v.mapping("limits")
    v.mapping("semantics")
    if "next_step" in body and body["next_step"] is not None:
        ns = _V(body["next_step"], f"{path}.next_step", errs)
        ns.keys(NEXT_STEP_FIELDS)
        for f in NEXT_STEP_FIELDS:
            ns.text(f)
    has_of = body.get("repeat_of") is not None
    has_reason = body.get("repeat_reason") is not None
    if has_of:
        v.token("repeat_of")
    if has_reason:
        v.text("repeat_reason")
    if has_of != has_reason:
        errs.append(f"{path}: repeat_of and repeat_reason must be given together")
    # Runtime-enforced bound (plan §1 Q3, §4.5): every Nous request carries
    # max_iterations; Nous enforces it.
    if body.get("capability") == "nous" and isinstance(body.get("limits"), dict):
        mi = body["limits"].get("max_iterations")
        if not isinstance(mi, int) or isinstance(mi, bool) or mi < 1:
            errs.append(f"{path}.limits.max_iterations: required integer >= 1 for nous")


def _receipt(body, path, errs, env_msg):
    v = _V(body, path, errs)
    v.keys(("received_message_id", "received_schema", "payload_digest",
            "status", "work_state", "reasons", "received_at"))
    v.token("received_message_id")
    v.text("received_schema")
    v.text("payload_digest")
    v.enum("status", RECEIPT_STATUSES)
    v.enum("work_state", LIFECYCLE_STATES, nullable=True)
    v.array("reasons", _text_item)
    v.timestamp("received_at")
    if v.ok and body.get("status") in ("rejected", "conflict") and not body.get("reasons"):
        errs.append(f"{path}.reasons: required when status is {body['status']}")
    if env_msg.get("reply_to") is None:
        errs.append("reply_to: required for a receipt")


def _progress(body, path, errs, env_msg):
    v = _V(body, path, errs)
    v.keys(("state", "at"), ("invocation_id", "detail"))
    v.enum("state", LIFECYCLE_STATES)
    v.timestamp("at")
    v.token("invocation_id")
    v.text("detail", nullable=True)


def _clarification(body, path, errs, env_msg):
    v = _V(body, path, errs)
    v.keys(("kind", "text"), ("fields",))
    v.enum("kind", ("question", "answer"))
    v.text("text")
    v.array("fields", _text_item)
    if v.ok and body.get("kind") == "answer" and env_msg.get("reply_to") is None:
        errs.append("reply_to: required for a clarification answer")


def _cancel(body, path, errs, env_msg):
    v = _V(body, path, errs)
    v.keys(("reason",), ("invocation_id", "interrupt"))
    v.text("reason")
    v.token("invocation_id")
    v.boolean("interrupt")


def _result_limitation(x, path, errs):
    v = _V(x, path, errs)
    v.keys(("source", "text"), ("artifact",))
    v.text("source")
    v.text("text")
    if v.ok and "artifact" in x:
        errs.extend(validate_artifact_ref(x["artifact"], f"{path}.artifact"))


def _result_limit(x, path, errs):
    v = _V(x, path, errs)
    v.keys(("name", "value", "enforced_by"), ("observed", "note"))
    v.text("name")
    v.enum("enforced_by", ENFORCED_BY)
    v.text("note")


def _result_claim(x, path, errs):
    # The answer contract's five keys, nothing else (validate_answer.py checks
    # the evidence; this only keeps draft-only fields out of the record).
    v = _V(x, path, errs)
    v.keys(CLAIM_KEYS)
    v.text("id")


def _result_hypothesis(x, path, errs):
    v = _V(x, path, errs)
    v.keys(("id", "classification", "partial"),
           ("iteration", "native_status", "error_type", "statement_ref",
            "findings_ref", "partial_note"))
    v.enum("classification", OUTCOMES + ("unclassified",))
    v.boolean("partial")


def _result_candidate(x, path, errs):
    # Candidate ids and locations only: estimates never enter the record.
    v = _V(x, path, errs)
    v.keys(("id",), ("rank", "module", "file", "cited"))
    v.text("id")


def _experiment_result(body, path, errs, env_msg):
    v = _V(body, path, errs)
    v.keys(("invocation_id", "execution", "collection", "outcome", "validation",
            "methods", "limitations", "limits", "spend", "versions"), ("findings",))
    if not v.ok:
        return
    v.token("invocation_id")
    ex_state = body.get("execution", {}).get("state") if isinstance(
        body.get("execution"), dict) else None
    col_state = body.get("collection", {}).get("state") if isinstance(
        body.get("collection"), dict) else None

    ex = _V(body.get("execution"), f"{path}.execution", errs) if "execution" in body else None
    if ex:
        ex.keys(("state", "native_stop_reason", "exit_code", "started_at",
                 "ended_at", "diagnostics"))
        ex.enum("state", LIFECYCLE_STATES)
        ex.text("native_stop_reason", nullable=True)
        if ex.ok and "exit_code" in ex.obj and ex.obj["exit_code"] != "unknown":
            ex.integer("exit_code", nullable=True)
        ex.timestamp("started_at", nullable=True)
        ex.timestamp("ended_at", nullable=True)
        ex.array("diagnostics", _ref_item)

    co = _V(body.get("collection"), f"{path}.collection", errs) \
        if "collection" in body else None
    if co:
        co.keys(("state", "reason"))
        co.enum("state", COLLECTION_STATES)
        co.text("reason", nullable=True)
        if co.ok and co.obj.get("state") != "collected" and not (
                isinstance(co.obj.get("reason"), str) and co.obj["reason"].strip()):
            errs.append(f"{path}.collection.reason: required unless collected")

    oc = _V(body.get("outcome"), f"{path}.outcome", errs) if "outcome" in body else None
    if oc:
        oc.keys(("classification", "hypothesis_ref", "explanation", "partial",
                 "classified_by"))
        oc.enum("classification", OUTCOMES)
        oc.text("hypothesis_ref", nullable=True)
        oc.text("explanation", nullable=True)
        oc.boolean("partial")
        oc.enum("classified_by", ("runtime", "coordinator"))
        cls = oc.obj.get("classification") if oc.ok else None
        if cls in ("unavailable", "other") and not (
                isinstance(oc.obj.get("explanation"), str) and oc.obj["explanation"].strip()):
            errs.append(f"{path}.outcome.explanation: required for {cls}")
        if cls in OUTCOMES and cls != "unavailable" and (
                ex_state != "completed" or col_state != "collected"):
            # spec §11: never a scientific verdict without a completed,
            # collected execution behind it.
            errs.append(f"{path}.outcome.classification: {cls} needs execution.state "
                        "completed and collection.state collected (else unavailable)")
        if cls in HYPOTHESIS_OUTCOMES and oc.obj.get("hypothesis_ref") is None:
            errs.append(f"{path}.outcome.hypothesis_ref: required for {cls} (the "
                        "hypothesis being classified)")

    if body.get("findings") is not None:
        fd = _V(body["findings"], f"{path}.findings", errs)
        fd.keys(("claims_from", "partial", "claims"),
                ("answer_ref", "hypotheses", "candidates"))
        fd.enum("claims_from", ("answer-contract/v1",))
        fd.boolean("partial")
        fd.array("claims", _result_claim)
        fd.array("hypotheses", _result_hypothesis)
        fd.array("candidates", _result_candidate)
        if fd.ok and "answer_ref" in fd.obj:
            errs.extend(validate_artifact_ref(fd.obj["answer_ref"],
                                              f"{path}.findings.answer_ref"))
        partial = fd.ok and fd.obj.get("partial") is True
        # spec §11: failure records never carry fabricated findings. A failed
        # or cancelled run keeps native findings only labelled partial (D3).
        if not (ex_state == "completed" or (ex_state in ("failed", "cancelled")
                                            and partial)):
            errs.append(f"{path}.findings: only allowed when execution.state is "
                        "completed, or failed/cancelled with findings.partial true")
        if col_state != "collected":
            errs.append(f"{path}.findings: only allowed when collection.state is "
                        "collected")

    va = _V(body.get("validation"), f"{path}.validation", errs) if "validation" in body else None
    if va:
        va.keys(("state", "validator"), ("report_ref", "answer_ref", "exit_code", "reason"))
        va.enum("state", VALIDATION_STATES)
        va.text("validator")
        va.integer("exit_code", nullable=True)
        va.text("reason", nullable=True)
        for k in ("report_ref", "answer_ref"):
            if va.ok and k in va.obj:
                errs.extend(validate_artifact_ref(va.obj[k], f"{path}.validation.{k}"))
        if va.ok and va.obj.get("state") == "passed" and va.obj.get("exit_code") not in (0,):
            errs.append(f"{path}.validation.exit_code: passed means the validator "
                        "exited 0")

    v.mapping("methods")
    v.array("limitations", _result_limitation)
    v.array("limits", _result_limit)

    sp = _V(body.get("spend"), f"{path}.spend", errs) if "spend" in body else None
    if sp:
        sp.keys(("reported_usd", "completeness", "source"), ("note",))
        sp.number("reported_usd", nullable=True)
        sp.enum("completeness", SPEND_COMPLETENESS)
        sp.text("source", nullable=True)
        sp.text("note", nullable=True)
        if sp.ok and sp.obj.get("completeness") == "unknown" \
                and sp.obj.get("reported_usd") not in (None,):
            errs.append(f"{path}.spend.reported_usd: must be null when completeness "
                        "is unknown (unknown spend is not zero)")

    vs = _V(body.get("versions"), f"{path}.versions", errs) if "versions" in body else None
    if vs:
        vs.keys(("runtime", "adapter", "engine_sha"))
        vs.text("runtime")
        vs.text("adapter")
        vs.text("engine_sha", nullable=True)


BODY_VALIDATORS = {
    "experiment-request": _experiment_request,
    "receipt": _receipt,
    "progress": _progress,
    "clarification": _clarification,
    "cancel": _cancel,
    "experiment-result": _experiment_result,
}
assert set(BODY_VALIDATORS) == set(SCHEMA_VERSIONS)


# --- envelope ---------------------------------------------------------------

def validate_message(msg: Any, env: Mapping[str, str] | None = None) -> list[str]:
    """All contract errors in msg ([] when valid). env defaults to os.environ."""
    if not isinstance(msg, dict):
        return ["top level: must be an object"]
    errs: list[str] = []
    try:
        mtype, _ = parse_schema(msg.get("schema"))
    except ContractError as e:
        errs.extend(e.errors)
        mtype = None
    v = _V(msg, "$", errs)
    v.keys(ENVELOPE_REQUIRED, ENVELOPE_OPTIONAL)
    v.token("message_id")
    v.token("work_id")
    v.integer("revision", minimum=1)
    if msg.get("reply_to") is not None:
        v.token("reply_to")
    if "target" in msg:
        t = _V(msg["target"], "$.target", errs)
        t.keys(("repo_name", "commit"))
        t.text("repo_name")
        t.token("commit", COMMIT_RE)
    v.array("artifacts", _ref_item)
    if mtype and "body" in msg:
        BODY_VALIDATORS[mtype](msg["body"], "$.body", errs, msg)
    errs.extend(find_secret_values(msg, env))
    return errs


def check_message(msg: Any, env: Mapping[str, str] | None = None) -> dict:
    errs = validate_message(msg, env)
    if errs:
        raise ContractError(errs)
    return msg


def message_type(msg: dict) -> str:
    return parse_schema(msg.get("schema"))[0]
