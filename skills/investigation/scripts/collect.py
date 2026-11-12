#!/usr/bin/env python3
"""Collect a Nous v2 campaign work dir into one JSON bundle for the ledger.

Nous v2 (branch origin/nous-v2-fixes of agentic-strategy-evolution) is a
rewrite: the work dir holds state.json (flat: phase, iteration, session_id,
baseline_status, stop_reason), ledger.json (a LIST, one row per iteration),
campaign.yaml, answer.json, final_check.json, baseline.json, report.md,
principles.json, runs.jsonl and runs/iter-N/. This collector reads a work
dir READ-ONLY and emits one bundle describing what the campaign concluded
and how far its own checks got.

A guard failure is information, not an error: the bundle is still produced
(exit 0) with `usable: false` and the reason. v2 itself refuses unevidenced
numbers (final_check.json), so this collector carries Nous's verdict instead
of recomputing it — the v0.5.0 silent-zero guard logic (best_found.json /
meta_findings.json) is deliberately NOT ported; v2 deleted all of that.

  guard 1  finished            state.phase is REPORT, or stop_reason is set
  guard 2  objective declared  campaign.yaml declares objective.metric +
                               objective.direction
  guard 3  answer checked      final_check.json exists (carried verbatim);
                               answer_valid false or file missing => unusable
  guard 4  baseline            baseline.json exists; stop_reason no_baseline
                               is carried as the reason, not a crash
  guard 5  read-only           the work dir is only ever opened for reading

Usage: python3 scripts/collect.py <work_dir> [--study DIR] [--runs-dir DIR]

--runs-dir (default: _config.runs_root(), i.e. $INVESTIGATION_RUNS, else the
machine config's runs_dir, else ~/.cache/investigation) is only used to
resolve a RELATIVE work_dir that does not exist relative to the current
directory: it is then tried as <runs-dir>/<work_dir>, so
`collect.py <repo>/<slug>/nous/<run>` works from anywhere. An absolute
work_dir, or a relative one that exists from the cwd, is used as given.

Exit 0 = collected (guards may have failed — carried in the bundle). Exit 1
= not a v2 work dir at all: not a directory, no readable state.json, a
state.json with `last_entered_phase` instead of `phase` (a v0.5.0 work dir),
a ledger.json present but unreadable, or a --study DIR that is not a
directory. With --study exactly ONE file is written,
<study>/collection.json; without --study the bundle goes to stdout.

--study MERGES (collection_version 2, see _collection.py): the bundle goes
under the "nous" key and every other top-level key (e.g. "telemetry",
"spotlights") is kept. Stdout mode still prints the bare bundle.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.dont_write_bytecode = True  # an install inside a target repo must not dirty it (D4)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _collection  # noqa: E402
import _config  # noqa: E402

ITER_DIR_RE = re.compile(r"\Aiter-(\d+)\Z")

ARTIFACT_FILES = (
    ("state", "state.json"), ("ledger", "ledger.json"), ("answer", "answer.json"),
    ("final_check", "final_check.json"), ("baseline", "baseline.json"),
    ("report", "report.md"), ("campaign_yaml", "campaign.yaml"),
)


class CouldNotRun(Exception):
    """The directory is not a v2 work dir at all: exit 1, message on stderr."""


# ── readers ──────────────────────────────────────────────────────────────


def load_json(path: Path):
    """Read one JSON artifact, read-only. Returns (data, error): data is None
    when the file is missing or unparseable; error names the parse failure (a
    missing file is a guard condition, not an error, for these artifacts)."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None, None
    except OSError as e:
        return None, f"{path.name} is unreadable: {e}"
    try:
        return json.loads(text), None
    except ValueError as e:
        return None, f"{path.name} is not valid JSON: {e}"


def load_yaml(path: Path):
    """Same contract as load_json, for the campaign config."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None, None
    except OSError as e:
        return None, f"{path.name} is unreadable: {e}"
    try:
        return yaml.safe_load(text), None
    except yaml.YAMLError as e:
        return None, f"{path.name} is not valid YAML: {str(e)[-200:]}"


# ── claims ───────────────────────────────────────────────────────────────


def rebase_evidence(work_dir: Path, evidence):
    """v2 exports evidence as repo-relative paths; resolve a relative path
    against the work dir into an absolute one. A bare string stays a string;
    a {path, key, value} detail mapping keeps its shape with path rebased;
    anything else is passed through unchanged."""
    if isinstance(evidence, str):
        p = Path(evidence)
        return str(p if p.is_absolute() else work_dir / p)
    if isinstance(evidence, dict) and isinstance(evidence.get("path"), str):
        out = dict(evidence)
        p = Path(evidence["path"])
        out["path"] = str(p if p.is_absolute() else work_dir / p)
        return out
    return evidence


# A Nous v2 evidence error names what it flags one of three ways: an explicit
# "claim <ID>:" prefix, a "<path>#<key>" json pointer, or a bare file name
# (formats pinned in tests/fixtures/workdir-answer-invalid/final_check.json and
# catalogued in tests/fixtures/workdir-example/README.md).
POINTER_RE = re.compile(r"(\S+)#(\S+?)(?=\s*:|\s|$)")
CLAIM_ID_RE = re.compile(r"\bclaim\s+([A-Za-z][A-Za-z0-9_-]*\d[A-Za-z0-9_-]*"
                         r"|[A-Za-z][A-Za-z0-9_-]{2,})\s*:")
# Whole-answer verdicts (the README's other error families): they blame the
# answer's shape, not any one claim, so they taint nobody.
WHOLE_ANSWER_RE = re.compile(
    r"\b(repro must not start with cd|claims_needed ids may be added, not removed"
    r"|wait_for\b|\|effect\||noise_sd)\b")


def _token_bounded(token: str, text: str) -> bool:
    """token appears in text as a whole word: not glued to another id-ish
    character (C1 must not match inside C12, hit_rate not inside hit_rates)."""
    if not token:
        return False
    return re.search(rf"(?<![A-Za-z0-9_-]){re.escape(token)}(?![A-Za-z0-9_-])",
                     text) is not None


def _named_claim_id(err: str):
    """The explicit 'claim <ID>:' attribution, when the error carries one."""
    m = CLAIM_ID_RE.search(err)
    return m.group(1) if m else None


def _pointer_of(err: str):
    """(path, key) of a '<path>#<key>' json-pointer reference, when present."""
    m = POINTER_RE.search(err)
    return (m.group(1), m.group(2)) if m else None


def _claim_evidence_path(claim) -> str:
    """The claim's own evidence path (string or {path} mapping), if any."""
    ev = claim.get("evidence")
    if isinstance(ev, str):
        return ev
    if isinstance(ev, dict) and isinstance(ev.get("path"), str):
        return ev["path"]
    detail = claim.get("evidence_detail")
    if isinstance(detail, dict) and isinstance(detail.get("path"), str):
        return detail["path"]
    return ""


def _names_file(err: str, path) -> bool:
    """The error names the claim's evidence file by its basename, as a word
    (the '.' in the tail keeps prose from matching a file-shaped token)."""
    if not path:
        return False
    base = Path(str(path)).name
    if not base or "." not in base:
        return False
    return _token_bounded(base, err)


def _cites(claim: dict, pointer) -> bool:
    """Does the claim cite the pointer's file basename AND key? A claim with
    evidence_detail {path,key} (or a {path,key} evidence mapping) is matched
    on both; a claim with a bare-string evidence path is matched on the
    basename plus its metric (the value-mismatch family carries no claim id,
    so the metric naming the pointer's key is the tie-breaker); a claim with
    no evidence path at all is tainted when its metric IS the pointer's key.
    The pointer's key is a json-pointer ('/p99_ms'), so keys and metrics are
    compared after stripping the leading slash. Stricter than the old
    metric-substring rule: an error pinning r9.json#/p99_ms never taints a
    sibling that merely shares the metric name but cites another file."""
    ppath, pkey = pointer
    pbase = Path(ppath).name
    pkey = pkey.lstrip("/")

    def key_matches(k) -> bool:
        return str(k or "").lstrip("/") == pkey

    detail = claim.get("evidence_detail")
    ev = claim.get("evidence")
    if isinstance(detail, dict):
        dpath = detail.get("path")
        return (isinstance(dpath, str) and Path(dpath).name == pbase
                and key_matches(detail.get("key")))
    if isinstance(ev, dict):
        epath = ev.get("path")
        return (isinstance(epath, str) and Path(epath).name == pbase
                and key_matches(ev.get("key")))
    ev_path = ev if isinstance(ev, str) else None
    metric = str(claim.get("metric") or "")
    if ev_path is not None:
        return Path(ev_path).name == pbase and metric == pkey
    return bool(metric) and metric == pkey


def _attributable(err: str) -> bool:
    """Can the error name any claim at all (an id, a pointer, or a file)?"""
    if _named_claim_id(err) is not None:
        return True
    if _pointer_of(err) is not None:
        return True
    return bool(re.search(r"\b[\w.-]+\.[A-Za-z0-9]{1,4}\b", err))


def claim_verified(claim: dict, evidence_errors: list) -> bool:
    """False when a final_check evidence_errors entry flags THIS claim; True
    when it stands as Nous checked it. An error is attributed by, in order:
    an explicit 'claim <ID>:' prefix (which decides alone), a '<path>#<key>'
    json pointer (taints the claims citing that file and key), or a bare file
    name (taints the claims citing that file). An error attributable to no
    claim conservatively taints every claim — a silent verified:true is the
    failure mode this guard exists to prevent."""
    cid = str(claim.get("id") or "")
    for err in evidence_errors:
        if not isinstance(err, str):
            continue
        named = _named_claim_id(err)
        if named is not None:
            # An explicit attribution decides alone: the fixture's C1/C2 share
            # R1.json, and 'claim C1:' must not taint C2.
            if named == cid:
                return False
            continue
        pointer = _pointer_of(err)
        if pointer is not None:
            if _cites(claim, pointer):
                return False
            continue
        if _token_bounded(cid, err):
            return False
        if _names_file(err, _claim_evidence_path(claim)):
            return False
        if not _attributable(err) and not WHOLE_ANSWER_RE.search(err):
            return False
    return True


def draft_claims(work_dir: Path, answer, evidence_errors: list) -> list:
    """The claims from the work dir's own answer.json, evidence rebased to
    absolute paths, each marked verified per final_check.evidence_errors."""
    claims = answer.get("claims") if isinstance(answer, dict) else None
    if not isinstance(claims, list):
        return []
    out = []
    for c in claims:
        if not isinstance(c, dict):
            continue
        ev = c.get("evidence")
        path = (rebase_evidence(work_dir, ev).get("path") if isinstance(ev, dict)
                else rebase_evidence(work_dir, ev))
        entry = {
            "id": c.get("id"), "metric": c.get("metric"), "value": c.get("value"),
            "condition": c.get("condition"), "evidence": path,
            "verified": claim_verified(c, evidence_errors),
        }
        detail = c.get("evidence_detail")
        if isinstance(detail, dict):
            entry["evidence_detail"] = rebase_evidence(work_dir, detail)
        out.append(entry)
    return out


def latest_iter_dir(work_dir: Path):
    """The highest-numbered runs/iter-N directory, or None when there is none."""
    runs = work_dir / "runs"
    try:
        names = os.listdir(runs)
    except OSError:
        return None
    best = None
    for name in names:
        m = ITER_DIR_RE.fullmatch(name)
        if m and (runs / name).is_dir():
            n = int(m.group(1))
            if best is None or n > best[0]:
                best = (n, runs / name)
    return best[1] if best else None


# ── bundle ───────────────────────────────────────────────────────────────


def build_bundle(work_dir: Path) -> dict:
    """Read the v2 work dir at `work_dir` (read-only) and build the bundle.
    Raises CouldNotRun when the directory is not a v2 work dir at all; a
    guard failure is a normal outcome carried inside the bundle."""
    state, state_err = load_json(work_dir / "state.json")
    if state is None:
        raise CouldNotRun(f"no readable state.json in {work_dir}"
                          + (f": {state_err}" if state_err else ""))
    if not isinstance(state, dict):
        raise CouldNotRun(f"state.json in {work_dir} is not a JSON object")
    if "phase" not in state:
        if "last_entered_phase" in state:
            raise CouldNotRun(f"{work_dir} is a pre-v2 (Nous v0.5.0) work dir: state.json "
                              "has 'last_entered_phase' instead of 'phase'; this collector "
                              "reads v2 work dirs only")
        raise CouldNotRun(f"state.json in {work_dir} has no 'phase' key; not a Nous v2 work dir")

    ledger_path = work_dir / "ledger.json"
    if ledger_path.is_file():
        ledger, ledger_err = load_json(ledger_path)
        if ledger_err or not isinstance(ledger, list):
            raise CouldNotRun(f"ledger.json in {work_dir} is unreadable"
                              + (f": {ledger_err}" if ledger_err else " (not a JSON list)"))

    phase = state.get("phase")
    iteration = state.get("iteration")
    stop_reason = state.get("stop_reason")
    guards: list[dict] = []

    def add(label: str, ok: bool, detail: str) -> None:
        guards.append({"label": label, "ok": ok, "detail": detail})

    # guard 1: finished — REPORT reached, or the loop stopped for any reason.
    if phase == "REPORT" or stop_reason is not None:
        detail = f"phase {phase}" + (f", stop_reason {stop_reason}" if stop_reason else "")
        add("finished", True, detail)
    else:
        add("finished", False, f"campaign still running (phase {phase}, iteration {iteration})")

    # guard 2: objective declared.
    campaign, camp_err = load_yaml(work_dir / "campaign.yaml")
    objective = None
    run_id = work_dir.name
    if camp_err:
        add("objective declared", False, camp_err)
    elif campaign is None:
        add("objective declared", False, "campaign.yaml is absent from the work dir")
    elif not isinstance(campaign, dict):
        add("objective declared", False, "campaign.yaml is not a mapping")
    else:
        rid = campaign.get("run_id")
        if isinstance(rid, str) and rid:
            run_id = rid
        obj = campaign.get("objective")
        metric = obj.get("metric") if isinstance(obj, dict) else None
        direction = obj.get("direction") if isinstance(obj, dict) else None
        if isinstance(metric, str) and metric and isinstance(direction, str) and direction:
            objective = {"metric": metric, "direction": direction}
            add("objective declared", True, f"objective {metric} {direction} declared in campaign.yaml")
        else:
            add("objective declared", False, "campaign.yaml declares no objective.metric + objective.direction")

    # guard 3: answer checked — carry Nous's own verdict, never recompute it.
    final_check, fc_err = load_json(work_dir / "final_check.json")
    if fc_err:
        add("answer checked", False, fc_err)
        final_check = None
    elif final_check is None:
        add("answer checked", False, "final_check.json is absent; Nous has not checked the answer")
    elif not isinstance(final_check, dict):
        add("answer checked", False, "final_check.json is not a JSON object")
        final_check = None
    elif final_check.get("answer_valid") is not True:
        errors = final_check.get("evidence_errors")
        errors = errors if isinstance(errors, list) else []
        first = errors[0] if errors and isinstance(errors[0], str) else "no detail"
        add("answer checked", False, f"final_check.answer_valid is false "
                                     f"({len(errors)} evidence error(s); first: {first})")
    else:
        add("answer checked", True, "final_check.json present, answer_valid true")

    # guard 4: baseline — a no_baseline stop is a carried reason, not a crash.
    baseline, baseline_err = load_json(work_dir / "baseline.json")
    baseline_block = {
        "present": baseline is not None,
        "task_type": baseline.get("task_type") if isinstance(baseline, dict) else None,
        "baseline_status": state.get("baseline_status"),
        "stop_reason": "no_baseline" if stop_reason == "no_baseline" else None,
    }
    if baseline_err:
        add("baseline", False, baseline_err)
    elif baseline is None:
        note = " (stop_reason no_baseline)" if stop_reason == "no_baseline" else ""
        add("baseline", False, f"baseline.json is absent from the work dir{note}")
    elif not isinstance(baseline, dict):
        add("baseline", False, "baseline.json is not a JSON object")
    else:
        add("baseline", True, f"baseline.json present (task_type {baseline.get('task_type')})")

    # guard 5: read-only — by construction; proven by the mtime test.
    add("read-only", True, "the work dir is opened for reading only; this collector "
                           "writes nothing but the bundle")

    failures = [g_["detail"] for g_ in guards if not g_["ok"]]

    answer, _ = load_json(work_dir / "answer.json")
    answer_summary = None
    if isinstance(answer, dict) and isinstance(answer.get("answer"), str):
        answer_summary = answer["answer"][:200]
    errs = final_check.get("evidence_errors") if isinstance(final_check, dict) else None
    claims = draft_claims(work_dir, answer, errs if isinstance(errs, list) else [])

    latest_findings = None
    idir = latest_iter_dir(work_dir)
    if idir is not None:
        findings, _ = load_json(idir / "findings.json")
        if isinstance(findings, dict):
            m = ITER_DIR_RE.fullmatch(idir.name)
            latest_findings = {"iteration": int(m.group(1)) if m else None,
                               "best": findings.get("best"),
                               "question_answered": findings.get("question_answered")}

    artifacts = {key: str(work_dir / name) if (work_dir / name).is_file() else None
                 for key, name in ARTIFACT_FILES}

    return {
        "run_id": run_id,
        "phase": phase,
        "iteration": iteration,
        "stop_reason": stop_reason,
        "work_dir": str(work_dir),
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "guards": guards,
        "usable": not failures,
        "reason": "; ".join(failures) if failures else None,
        "objective": objective,
        "answer_summary": answer_summary,
        "final_check": final_check,
        "baseline": baseline_block,
        "claims_draft": claims,
        "latest_findings": latest_findings,
        "artifacts": artifacts,
    }


# ── normalized findings, limitations, methods (runtime plan §1 Q4) ──────
#
# Used by the runtime's Nous adapter (ains_runtime/adapters/nous.py); the
# CLI above does not call these, so its bundle is unchanged. Read-only.

# findings.json status -> normalized hypothesis classification (decision D2:
# PARTIALLY_CONFIRMED is `other`, carrying Nous's partial_note).
STATUS_CLASSIFICATION = {
    "CONFIRMED": "supported",
    "REFUTED": "not_supported",
    "INCONCLUSIVE": "inconclusive",
    "PARTIALLY_CONFIRMED": "other",
}

COST_INCOMPLETE = ("Nous cost is incomplete: BASELINE cost is folded into iteration 1, "
                   "a crashed iteration has no ledger row, the no-baseline path records "
                   "nothing, and models missing from the price table count as $0")


def iter_dirs(work_dir: Path) -> list:
    """[(N, runs/iter-N)] for every iteration directory, ascending."""
    try:
        names = os.listdir(work_dir / "runs")
    except OSError:
        return []
    out = []
    for name in names:
        m = ITER_DIR_RE.fullmatch(name)
        if m and (work_dir / "runs" / name).is_dir():
            out.append((int(m.group(1)), work_dir / "runs" / name))
    return sorted(out)


def _hypothesis_statements(idir: Path):
    """{id: hypothesis} from the locked bundle (preferred) or bundle.json."""
    for name in (".bundle.locked.json", "bundle.json"):
        data, _ = load_json(idir / name)
        if isinstance(data, dict) and isinstance(data.get("hypotheses"), list):
            return ({h["id"]: h for h in data["hypotheses"]
                     if isinstance(h, dict) and isinstance(h.get("id"), str)},
                    idir / name)
    return {}, None


def hypotheses(work_dir: Path) -> list:
    """Every classified hypothesis, per iteration (Q4): statement from the
    bundle, classification from findings.json status."""
    out = []
    for n, idir in iter_dirs(work_dir):
        findings, _ = load_json(idir / "findings.json")
        if not isinstance(findings, dict) or not isinstance(findings.get("hypotheses"), list):
            continue
        stmts, stmt_path = _hypothesis_statements(idir)
        for h in findings["hypotheses"]:
            if not isinstance(h, dict):
                continue
            hid = h.get("id")
            b = stmts.get(hid) if isinstance(hid, str) else None
            status = h.get("status")
            entry = {
                "id": hid, "iteration": n,
                "statement": ({k: b.get(k) for k in ("role", "family", "mechanism",
                                                     "prediction", "falsified_if")}
                              if isinstance(b, dict) else None),
                "statement_ref": str(stmt_path) if b is not None and stmt_path else None,
                "native_status": status,
                "classification": STATUS_CLASSIFICATION.get(status, "unclassified"),
                "error_type": h.get("error_type"),
                "findings_ref": str(idir / "findings.json"),
            }
            if status == "PARTIALLY_CONFIRMED":
                entry["partial_note"] = h.get("partial_note")
            out.append(entry)
    return out


def limitations(work_dir: Path) -> list:
    """Limitations derived from native artifacts, each labelled with its source
    (Q4: no Nous artifact has a `limitations` field)."""
    out = []

    def add(source, text, location):
        out.append({"source": f"nous:{source}", "text": str(text), "location": str(location)})

    for n, idir in iter_dirs(work_dir):
        f, _ = load_json(idir / "findings.json")
        if not isinstance(f, dict):
            continue
        if isinstance(f.get("reason"), str) and f["reason"]:
            add("findings.reason", f"iteration {n}: {f['reason']}", idir / "findings.json")
        for h in f.get("hypotheses") or []:
            if isinstance(h, dict) and h.get("noise_unmeasured") is True:
                add("findings.noise_unmeasured",
                    f"iteration {n} {h.get('id')}: noise unmeasured"
                    + (f" ({h['noise_reason']})" if h.get("noise_reason") else "")
                    + "; the 2x noise check was skipped", idir / "findings.json")
    fc, _ = load_json(work_dir / "final_check.json")
    if isinstance(fc, dict):
        un = fc.get("unmeasured_noise_findings")
        if isinstance(un, list) and un:
            add("final_check.unmeasured_noise_findings",
                f"hypotheses with unmeasured noise: {', '.join(map(str, un))}",
                work_dir / "final_check.json")
        if fc.get("answer_valid") is not True:
            errs = fc.get("evidence_errors") if isinstance(fc.get("evidence_errors"), list) else []
            add("final_check.answer_valid",
                f"answer_valid is {fc.get('answer_valid')!r}; {len(errs)} evidence error(s)"
                + (": " + "; ".join(map(str, errs[:5])) if errs else ""),
                work_dir / "final_check.json")
    halt, _ = load_json(work_dir / "halt.json")
    if (work_dir / "halt.json").is_file():
        h = halt if isinstance(halt, dict) else {}
        add("halt.json", f"halted after repeated identical failures in phase "
                         f"{h.get('phase', '?')}: {h.get('message', 'no message')}",
            work_dir / "halt.json")
    ledger, _ = load_json(work_dir / "ledger.json")
    for row in ledger if isinstance(ledger, list) else []:
        if not isinstance(row, dict):
            continue
        if row.get("status") == "partial":
            add("ledger.status", f"iteration {row.get('iteration')} is partial (validation "
                                 "errors remained after repair)", work_dir / "ledger.json")
        if row.get("prereg_violation") is True:
            add("ledger.prereg_violation", f"iteration {row.get('iteration')}: the bundle "
                "changed after its predictions were locked", work_dir / "ledger.json")
    base, _ = load_json(work_dir / "baseline.json")
    noise = base.get("noise") if isinstance(base, dict) else None
    if isinstance(noise, dict) and noise.get("unmeasured") is True:
        add("baseline.noise.unmeasured", "baseline noise unmeasured"
            + (f" ({noise['reason']})" if noise.get("reason") else ""),
            work_dir / "baseline.json")
    return out


def methods(work_dir: Path) -> dict:
    """What was measured, and how (Q4): baseline, planned runs, recorded
    experiment commands."""
    base, _ = load_json(work_dir / "baseline.json")
    baseline = None
    if isinstance(base, dict):
        baseline = {"command": base.get("command"), "git_sha": base.get("git_sha"),
                    "noise": base.get("noise"), "location": str(work_dir / "baseline.json")}
    plans = []
    for n, idir in iter_dirs(work_dir):
        plan, _ = load_json(idir / "plan.json")
        if isinstance(plan, dict) and isinstance(plan.get("runs"), list):
            plans.append({"iteration": n, "location": str(idir / "plan.json"),
                          "runs": [{k: r.get(k) for k in ("id", "hypothesis_id", "role",
                                                          "command")}
                                   for r in plan["runs"] if isinstance(r, dict)]})
    runs = []
    try:
        lines = (work_dir / "runs.jsonl").read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("experiment") is True:
            runs.append({k: row.get(k) for k in ("phase", "iter", "command", "exit_code")})
    return {"baseline": baseline, "plans": plans,
            "experiment_runs": runs,
            "experiment_runs_ref": str(work_dir / "runs.jsonl") if lines else None}


def spend(work_dir: Path) -> dict:
    """Reported spend from the ledger; never complete at the pin (Q3)."""
    ledger, _ = load_json(work_dir / "ledger.json")
    rows = [r for r in ledger if isinstance(r, dict)] if isinstance(ledger, list) else []
    if not rows:
        return {"reported_usd": None, "completeness": "unknown",
                "source": "nous ledger.json (no rows)", "note": COST_INCOMPLETE}
    total = 0.0
    for r in rows:
        for k in ("cost_meter_usd", "report_cost_usd"):
            v = r.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                total += v
    return {"reported_usd": round(total, 6), "completeness": "partial",
            "source": "nous ledger.json cost_meter_usd + report_cost_usd",
            "note": COST_INCOMPLETE}


def normalize(work_dir: Path) -> dict:
    """The collect.py bundle plus the Q4 normalization, in one read-only pass.
    Raises CouldNotRun like build_bundle."""
    return {"bundle": build_bundle(work_dir), "hypotheses": hypotheses(work_dir),
            "limitations": limitations(work_dir), "methods": methods(work_dir),
            "spend": spend(work_dir)}


# ── CLI ──────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("work_dir", help="path to the Nous v2 work dir (read-only)")
    parser.add_argument("--study", metavar="DIR", default=None,
                        help="merge the bundle into <DIR>/collection.json under \"nous\" "
                             "instead of printing it")
    parser.add_argument("--runs-dir", metavar="DIR", default=None,
                        help="runs root used to resolve a relative work_dir that is not "
                             "found from the cwd (default: $INVESTIGATION_RUNS, else the "
                             "machine config runs_dir, else ~/.cache/investigation)")
    args = parser.parse_args(argv)

    work_dir = Path(args.work_dir).expanduser()
    if not work_dir.is_absolute() and not work_dir.exists():
        runs = (Path(args.runs_dir).expanduser() if args.runs_dir is not None
                else _config.runs_root())
        if (runs / work_dir).is_dir():
            work_dir = runs / work_dir
    if not work_dir.is_dir():
        print(f"{work_dir} is not a directory", file=sys.stderr)
        return 1
    study = None
    if args.study is not None:
        study = Path(args.study).expanduser()
        if not study.is_dir():
            print(f"--study {study} is not a directory", file=sys.stderr)
            return 1
    work_dir = work_dir.resolve()
    study = study.resolve() if study is not None else None

    try:
        bundle = build_bundle(work_dir)
    except CouldNotRun as e:
        print(e, file=sys.stderr)
        return 1

    text = json.dumps(bundle, indent=2) + "\n"
    if study is None:
        sys.stdout.write(text)
    else:
        try:
            _collection.merge_section(study, "nous", bundle)
        except _collection.CollectionError as e:
            print(e, file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
