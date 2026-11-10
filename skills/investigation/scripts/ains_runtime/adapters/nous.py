"""Nous v2 adapter: one campaign in the BLIS simulator (plan §5 R4).

Engine facts come from references/upstreams/nous.json and blis.json, each
cited at the locked version in lock.json (plan §1 Q2-Q5, §2 G1/G6). The adapter never runs Nous itself except
`nous stop` from cancel(); the supervisor (R3) spawns `nous run`.

Request semantics (`body.semantics`; everything else in the body is common):

  target_system   str description, or {name?, description}  (required)
                  All domain context goes here (nous-blis.md); repo_path is
                  set by the adapter to the target toplevel.
  models          {session: <model>}                           (required; no
                  implicit default: Nous would use claude-opus-4-6, Q5)
  objective       {metric, direction: increase|decrease}       (required)
  blis            {knobs: [flag, ...], catalog?: <dir under the apparatus
                  root, default "blis-catalog">, limitations?: [text]}
                  knobs are the BLIS flags the question varies; each must be
                  BLIS-representable (method-plan.md). `limitations` are
                  coordinator-written and carried into collect() with
                  source "coordinator".                        (required)
  experiment_commands, task_limits, locked_workload, min_improvement,
  stall_iterations, effort, session_scope, secrets          (optional,
                  rendered as the campaign keys of the same name)
  run_id          NON-material: names the work dir and the nous/<run_id>
                  branch. Default: "<work-id>-<prep-id>" (long and unique).

`body.locks` becomes `locked_parameters` verbatim and must carry the
apparatus locks: `blis_binary` (path, absolute or relative to the apparatus
root), `blis_binary_sha256`, `catalog_commit`. validate() checks them
against the apparatus (nous-blis.md pre-launch checks).

`body.question` -> research_question, `body.task_type` -> task_type,
`limits.max_iterations` -> max_iterations. `body.objective`, `baseline` and
`hypothesis` are appended to the description so they reach the agent.

The rendered campaign.yaml carries only CAMPAIGN_KEYS (a subset of the
locked Nous's known keys, nous.json known_campaign_keys): never `prompts.*`,
`metadata`, `ground_truth`, `iterations`, `domain_adapter_layer` or
`human_gates` (ignored or unimplemented at the pin).

Injectable hooks (tests; nothing here needs Nous installed when they are
replaced): probe(ctx) -> availability dict, list_processes() -> [(pid,
cmdline)], run_cmd(argv, env) -> (rc, output).
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

from .base import Adapter, AdapterContext, LaunchSpec, NativeState, Prepared

_SCRIPTS = Path(__file__).resolve().parent.parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import upstreams as U  # noqa: E402  (references/upstreams/: the only home of upstream facts)

# Every fact about Nous and BLIS comes from references/upstreams/{nous,blis}.json,
# checked against the locked versions in lock.json. Nothing here restates them.
ENV_PARENT = U.fact("nous", "campaign_parent_env")
WORKTREE_DIR = U.fact("nous", "worktree_dir")
CAMPAIGN_KEYS = tuple(U.fact("nous", "campaign_keys"))
IGNORED_KEYS = tuple(U.fact("nous", "ignored_keys"))
TARGET_SYSTEM_KEYS = tuple(U.fact("nous", "target_system_keys"))
OPTIONAL_PASSTHROUGH = tuple(U.fact("nous", "optional_passthrough"))
EFFORT_LEVELS = tuple(U.fact("nous", "effort_levels"))
SESSION_SCOPES = tuple(U.fact("nous", "session_scopes"))
DIRECTIONS = tuple(U.fact("nous", "objective_directions"))
REQUIRED_LOCKS = tuple(U.fact("blis", "required_locks"))

# BLIS representability: a knob is representable only if it matches one of
# these; everything else is not established.
REPRESENTABLE_KNOBS = tuple((re.compile(r"\A(?:" + k["pattern"] + r")\Z"), k["label"])
                            for k in U.fact("blis", "representable_knobs"))
NOT_REPRESENTABLE = tuple((re.compile(k["pattern"], re.I), k["label"])
                          for k in U.fact("blis", "not_representable"))
REPRESENTABLE_SUMMARY = "; ".join(k["doc"] for k in U.fact("blis", "representable_knobs"))

RUN_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{7,99}\Z")
HEX40_RE = re.compile(r"\A[0-9a-f]{40}\Z")
HEX64_RE = re.compile(r"\A(sha256:)?[0-9a-f]{64}\Z")

COMPLETED_STOPS = tuple(U.fact("nous", "completed_stop_reasons"))
NATIVE_FILES = tuple(tuple(x) for x in U.fact("nous", "native_files"))
SIGINT_EXIT = U.fact("nous", "sigint_exit")


# --- default hooks ----------------------------------------------------------

def default_probe(ctx: AdapterContext) -> dict:
    """doctor.py's nous-blis availability, the installed engine sha, the pin
    and the apparatus root. Read-only; offline (no ls-remote)."""
    import _config
    import doctor
    try:
        machine = _config.load_machine_config()
    except _config.ConfigError as e:
        return {"available": False, "reason": f"machine config: {e}"}
    try:
        repo_cfg = _config.load_repo_config(ctx.target)
    except _config.ConfigError as e:
        return {"available": False, "reason": f"repo config: {e}"}
    pins = doctor.read_pins()
    nous = doctor.engine_info("nous", machine, pins, True, {})
    engines = {n: {"found": False} for n in doctor.ENGINES}
    engines["nous"] = nous
    root = machine.get("blis_apparatus_root")
    blis = {"apparatus_root": str(Path(str(root)).expanduser()) if root else None,
            "configured": bool(root),
            "exists": bool(root) and Path(str(root)).expanduser().is_dir()}
    tools = {"git": {"path": shutil.which("git")}, "kubectl": {"path": None},
             "pyyaml": {"available": _config.yaml_available()}}
    m = doctor.methods(tools, engines, blis, repo_cfg, {})["nous-blis"]
    _, pin_sha = doctor.split_pin(pins.get("nous-pin"))
    return {"available": bool(m.get("available")), "reason": m.get("reason"),
            "engine_sha": nous.get("sha"), "pin": pins.get("nous-pin"),
            "pin_sha": pin_sha, "apparatus_root": blis["apparatus_root"],
            "nous_on_path": shutil.which("nous", path=ctx.env.get("PATH")) is not None}


def default_list_processes() -> list[tuple[int, str]]:
    from .. import procid
    out: list[tuple[int, str]] = []
    if procid._proc_usable():
        for d in procid.PROC_ROOT.iterdir():
            if d.name.isdigit():
                cmd = procid._proc_cmdline(int(d.name))
                if cmd:
                    out.append((int(d.name), cmd))
        return out
    text = procid._run_ps(["-ax", "-ww", "-o", "pid=", "-o", "command="])
    if text is None:
        raise RuntimeError("cannot list processes (no /proc and `ps -ax` failed)")
    for line in text.splitlines():
        t = line.strip().split(None, 1)
        if len(t) == 2 and t[0].isdigit():
            out.append((int(t[0]), t[1]))
    return out


def default_run_cmd(argv: list[str], env: Mapping[str, str]) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, env=dict(env), timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return -1, str(e)
    return r.returncode, (r.stdout + r.stderr).strip()[-2000:]


# --- small readers -----------------------------------------------------------

def _read_json(path: Path):
    import json
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git(*args: str, cwd) -> str | None:
    import _config
    return _config.git(*args, cwd=cwd)


def _sem(request: dict) -> dict:
    return request.get("body", {}).get("semantics", {}) or {}


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-.")[:48] or "nous"


class NousAdapter(Adapter):
    capability = "nous"
    version = "nous-adapter-0.1.0"
    MATERIAL_FIELDS = ("target_system", "models", "objective", "blis",
                       "experiment_commands", "task_limits", "locked_workload",
                       "min_improvement", "stall_iterations", "effort", "session_scope",
                       "secrets")
    NON_MATERIAL_FIELDS = {"run_id": "names the work dir and the nous/<run_id> branch "
                                     "only; renders differing only in run_id are "
                                     "byte-identical apart from it (plan §4.2)"}

    def __init__(self, *, probe: Callable[[AdapterContext], dict] | None = None,
                 list_processes: Callable[[], list] | None = None,
                 run_cmd: Callable[[list, Mapping], tuple] | None = None):
        self.probe = probe or default_probe
        self.list_processes = list_processes or default_list_processes
        self.run_cmd = run_cmd or default_run_cmd

    # --- identity of the native run -----------------------------------------

    @staticmethod
    def run_id(request: dict, ctx: AdapterContext) -> str:
        rid = _sem(request).get("run_id")
        if rid:
            return str(rid)
        # Stable across prepare and revalidate (same prep dir), unique per prep.
        return f"{_safe(ctx.work_id)}-{ctx.prep_dir.name}"

    @staticmethod
    def work_dir(record: dict) -> Path:
        return Path(record["env_set"][ENV_PARENT]) / record["native_run_id"]

    # --- validate ------------------------------------------------------------

    def _apparatus_problems(self, request: dict, root: str | None) -> tuple[list[str], dict]:
        body = request["body"]
        locks = body.get("locks") or {}
        blis = _sem(request).get("blis")
        out: list[str] = []
        facts: dict = {}
        missing = [k for k in REQUIRED_LOCKS if not locks.get(k)]
        if missing:
            out += [f"locks.{k}: required (nous-blis.md: lock the BLIS binary path and "
                    "sha256, and the catalog commit)" for k in missing]
        out += self.lockfile_problems(locks, facts)
        if not root:
            out.append("no BLIS apparatus root (blis_apparatus_root in the machine config)")
            return out, facts
        root_p = Path(root)
        if locks.get("blis_binary"):
            b = Path(str(locks["blis_binary"])).expanduser()
            b = b if b.is_absolute() else root_p / b
            facts["binary"] = str(b)
            want = str(locks.get("blis_binary_sha256") or "")
            if want and not HEX64_RE.fullmatch(want):
                out.append("locks.blis_binary_sha256: must be 64 hex digits "
                           "(optionally prefixed sha256:)")
            if not b.is_file():
                out.append(f"BLIS binary {b} does not exist")
            else:
                have = sha256_file(b)
                facts["binary_sha256"] = have
                if want and HEX64_RE.fullmatch(want) and want.split(":")[-1] != have:
                    out.append(f"BLIS binary {b} sha256 is {have}, the lock says "
                               f"{want.split(':')[-1]}")
        cat_rel = (blis.get("catalog") if isinstance(blis, dict) else None) or "blis-catalog"
        cat = Path(str(cat_rel)).expanduser()
        cat = cat if cat.is_absolute() else root_p / cat
        facts["catalog"] = str(cat)
        if locks.get("catalog_commit"):
            want = str(locks["catalog_commit"])
            if not HEX40_RE.fullmatch(want):
                out.append("locks.catalog_commit: must be a full 40-hex commit")
            have = _git("rev-parse", "HEAD", cwd=cat) if cat.is_dir() else None
            if have is None:
                out.append(f"BLIS catalog {cat} is not a git checkout")
            else:
                facts["catalog_commit"] = have
                if HEX40_RE.fullmatch(want) and have != want:
                    out.append(f"BLIS catalog {cat} is at {have}, the lock says {want}")
        return out, facts

    @staticmethod
    def lockfile_problems(locks: dict, facts: dict) -> list[str]:
        """The request's BLIS binary and catalog must be ones lock.json names
        (references/upstreams/lock.json, upstreams.blis)."""
        out = []
        sha = str(locks.get("blis_binary_sha256") or "")
        if sha and HEX64_RE.fullmatch(sha):
            hit = U.blis_binary(sha)
            if hit is None:
                out.append(f"locks.blis_binary_sha256 {sha.split(':')[-1][:12]} is not a BLIS "
                           "binary in references/upstreams/lock.json; add the build there "
                           "(with its source commit, router and patches) before using it")
            else:
                facts["lock_binary_id"] = hit[0]
        cat = str(locks.get("catalog_commit") or "")
        want = U.entry("blis").get("catalog", {}).get("commit")
        if cat and HEX40_RE.fullmatch(cat) and not U.blis_catalog_locked(cat):
            out.append(f"locks.catalog_commit {cat[:12]} is not the catalog commit in "
                       f"references/upstreams/lock.json ({(want or '?')[:12]})")
        return out

    @staticmethod
    def representability_problems(request: dict) -> list[str]:
        """Reasons the question is not BLIS-representable ([] = ok)."""
        body = request["body"]
        out = []
        text = " ".join(str(body.get(k) or "") for k in ("question", "objective",
                                                         "hypothesis", "baseline"))
        for rx, label in NOT_REPRESENTABLE:
            if rx.search(text):
                out.append(f"not BLIS-representable: the request turns on {label}, which "
                           "BLIS can't represent (method-plan.md); nous-blis is excluded "
                           "for this question, never approximated")
        blis = _sem(request).get("blis")
        knobs = blis.get("knobs") if isinstance(blis, dict) else None
        if not (isinstance(knobs, list) and knobs and all(isinstance(k, str) for k in knobs)):
            out.append("semantics.blis.knobs: required non-empty list of the BLIS flags "
                       "the question varies")
            return out
        for k in knobs:
            if not any(rx.fullmatch(k) for rx, _ in REPRESENTABLE_KNOBS):
                out.append(f"not BLIS-representable: knob {k!r} is not in the representable "
                           f"set ({REPRESENTABLE_SUMMARY}; references/upstreams/blis.json)")
        return out

    @staticmethod
    def semantics_problems(request: dict) -> list[str]:
        body = request["body"]
        sem = _sem(request)
        out = []
        ts = sem.get("target_system")
        desc = ts if isinstance(ts, str) else (ts.get("description")
                                                if isinstance(ts, dict) else None)
        if not (isinstance(desc, str) and desc.strip()):
            out.append("semantics.target_system: required description (all domain "
                       "context goes in target_system.description)")
        if isinstance(ts, dict):
            extra = sorted(set(ts) - set(TARGET_SYSTEM_KEYS))
            if extra:
                out.append(f"semantics.target_system: keys {extra} are not rendered "
                           f"(allowed: {list(TARGET_SYSTEM_KEYS)}; repo_path is set by the "
                           "adapter)")
        models = sem.get("models")
        if not (isinstance(models, dict) and isinstance(models.get("session"), str)
                and models["session"].strip()):
            out.append("semantics.models.session: required (no implicit default)")
        elif set(models) - {"session"}:
            out.append("semantics.models: only `session` is supported")
        obj = sem.get("objective")
        if not (isinstance(obj, dict) and isinstance(obj.get("metric"), str)
                and obj["metric"].strip() and obj.get("direction") in DIRECTIONS
                and set(obj) <= {"metric", "direction"}):
            out.append("semantics.objective: required {metric, direction: "
                       "increase|decrease}")
        for k in ("question", "objective"):
            if not (isinstance(body.get(k), str) and body[k].strip()):
                out.append(f"{k}: required")
        mi = (body.get("limits") or {}).get("max_iterations")
        if isinstance(mi, bool) or not isinstance(mi, int) or mi < 1:
            out.append("limits.max_iterations: required integer >= 1")
        rid = sem.get("run_id")
        if rid is not None and not (isinstance(rid, str) and RUN_ID_RE.fullmatch(rid)):
            out.append("semantics.run_id: 8-100 chars of [A-Za-z0-9._-], starting with a "
                       "letter or digit (long ids keep the non-material templating exact)")
        ec = sem.get("experiment_commands")
        if ec is not None:
            if not (isinstance(ec, list) and ec and all(isinstance(x, str) for x in ec)):
                out.append("semantics.experiment_commands: non-empty list of regexes")
            else:
                for x in ec:
                    try:
                        re.compile(x)
                    except re.error as e:
                        out.append(f"semantics.experiment_commands: bad regex {x!r}: {e}")
        tl = sem.get("task_limits")
        if tl is not None and not (isinstance(tl, dict) and set(tl) <= {"max_runs"} and
                                   isinstance(tl.get("max_runs"), int) and
                                   not isinstance(tl.get("max_runs"), bool)):
            out.append("semantics.task_limits: only {max_runs: int}")
        lw = sem.get("locked_workload")
        if lw is not None and not isinstance(lw, dict):
            out.append("semantics.locked_workload: must be a flat mapping")
        for k in ("min_improvement",):
            v = sem.get(k)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                out.append(f"semantics.{k}: must be a number")
        v = sem.get("stall_iterations")
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 1):
            out.append("semantics.stall_iterations: integer >= 1")
        if sem.get("effort") is not None and sem["effort"] not in EFFORT_LEVELS:
            out.append(f"semantics.effort: one of {list(EFFORT_LEVELS)}")
        if sem.get("session_scope") is not None and sem["session_scope"] not in SESSION_SCOPES:
            out.append(f"semantics.session_scope: one of {list(SESSION_SCOPES)}")
        sv = sem.get("secrets")
        if sv is not None and not (isinstance(sv, list) and all(
                isinstance(x, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", x) for x in sv)):
            out.append("semantics.secrets: list of environment variable names")
        blis = sem.get("blis")
        if blis is not None and not isinstance(blis, dict):
            out.append("semantics.blis: must be a mapping")
        elif isinstance(blis, dict):
            extra = sorted(set(blis) - {"knobs", "catalog", "limitations"})
            if extra:
                out.append(f"semantics.blis: unknown keys {extra}")
            lim = blis.get("limitations")
            if lim is not None and not (isinstance(lim, list) and
                                        all(isinstance(x, str) for x in lim)):
                out.append("semantics.blis.limitations: list of text")
        return out

    def collision_problems(self, run_id: str, ctx: AdapterContext,
                           binary: str | None) -> list[str]:
        out = []
        wd = ctx.runs_dir / run_id
        if wd.exists():
            out.append(f"work dir {wd} already exists (a fresh launch needs a new run_id)")
        legacy = ctx.target / ".nous" / run_id / "state.json"
        if legacy.exists():
            out.append(f"a Nous campaign already exists at {legacy.parent}")
        wt = ctx.target / WORKTREE_DIR / run_id
        if wt.exists():
            out.append(f"Nous worktree {wt} already exists")
        if _git("rev-parse", "--verify", "--quiet", f"refs/heads/nous/{run_id}",
                cwd=ctx.target) is not None:
            out.append(f"branch nous/{run_id} already exists in the target (Nous would "
                       "reuse it as-is)")
        try:
            procs = self.list_processes()
        except Exception as e:
            return out + [f"cannot check for running processes: {e}"]
        me = os.getpid()
        for pid, cmd in procs:
            if pid == me:
                continue
            toks = cmd.split()
            nous_run = any(os.path.basename(t) == "nous" and i + 1 < len(toks)
                           and toks[i + 1] in ("run", "resume") for i, t in enumerate(toks))
            if nous_run or (binary and binary in cmd):
                out.append(f"process {pid} is already running ({cmd[:160]}); nous-blis "
                           "runs one campaign at a time")
        return out

    def validate(self, request: dict, ctx: AdapterContext) -> list[str]:
        out = self.semantics_problems(request)
        out += self.representability_problems(request)
        try:
            p = self.probe(ctx)
        except Exception as e:
            p = {"available": False, "reason": f"availability check failed: {e}"}
        if not p.get("available"):
            out.append(f"nous-blis unavailable: {p.get('reason') or 'unknown reason'}")
        sha, pin = p.get("engine_sha"), p.get("pin_sha")
        if not pin:
            out.append("no nous pin in references/upstreams/lock.json")
        elif not sha:
            out.append(f"cannot read the installed nous sha to compare with pin {pin}")
        elif not (len(pin) >= 7 and sha.startswith(pin)):
            out.append(f"installed nous {sha[:12]} differs from the pin {p.get('pin') or pin}")
        if p.get("available") and not p.get("nous_on_path"):
            out.append("`nous` is not on PATH (the supervisor spawns `nous run`)")
        app, facts = self._apparatus_problems(request, p.get("apparatus_root"))
        out += app
        out += self.collision_problems(self.run_id(request, ctx), ctx, facts.get("binary"))
        return out

    def versions(self, ctx: AdapterContext) -> dict:
        p = self.probe(ctx)
        return {"engine_sha": p.get("engine_sha"), "pin": p.get("pin")}

    # --- prepare -------------------------------------------------------------

    def render_campaign(self, request: dict, ctx: AdapterContext, run_id: str) -> dict:
        body = request["body"]
        sem = _sem(request)
        ts = sem["target_system"]
        ts = {"description": ts} if isinstance(ts, str) else dict(ts)
        desc = ts["description"].rstrip("\n")
        extra = ["", "## Request (rendered by the AINS runtime)", "",
                 f"- Objective: {body['objective']}"]
        if body.get("baseline"):
            extra.append(f"- Baseline: {body['baseline']}")
        if body.get("hypothesis"):
            extra.append(f"- Hypothesis under test: {body['hypothesis']}")
        extra.append("- The locked parameters below are the approved apparatus; never "
                     "modify the BLIS binary or the catalog.")
        target = {}
        if ts.get("name"):
            target["name"] = ts["name"]
        target["repo_path"] = str(ctx.target)
        target["description"] = desc + "\n" + "\n".join(extra) + "\n"
        doc: dict[str, Any] = {
            "research_question": body["question"],
            "run_id": run_id,
            "max_iterations": body["limits"]["max_iterations"],
            "task_type": body["task_type"],
            "models": {"session": sem["models"]["session"]},
            "target_system": target,
            "objective": {"metric": sem["objective"]["metric"],
                          "direction": sem["objective"]["direction"]},
        }
        if body.get("locks"):
            doc["locked_parameters"] = dict(body["locks"])
        for k in OPTIONAL_PASSTHROUGH:
            if sem.get(k) is not None:
                doc[k] = sem[k]
        assert set(doc) <= set(CAMPAIGN_KEYS), sorted(set(doc) - set(CAMPAIGN_KEYS))
        return doc

    @staticmethod
    def dump_yaml(doc: dict) -> bytes:
        import yaml

        class _D(yaml.SafeDumper):
            pass

        def _str(dumper, s):
            style = "|" if "\n" in s else None
            return dumper.represent_scalar("tag:yaml.org,2002:str", s, style=style)
        _D.add_representer(str, _str)
        text = yaml.dump(doc, Dumper=_D, sort_keys=False, allow_unicode=True,
                         default_flow_style=False, width=4096)
        return ("# Generated by the AINS runtime Nous adapter; read-only. "
                "Edit the request, not this file.\n" + text).encode("utf-8")

    def prepare(self, request: dict, ctx: AdapterContext) -> Prepared:
        body = request["body"]
        sem = _sem(request)
        run_id = self.run_id(request, ctx)
        p = self.probe(ctx)
        _, facts = self._apparatus_problems(request, p.get("apparatus_root"))
        doc = self.render_campaign(request, ctx, run_id)
        campaign = str(ctx.prep_dir / "campaign.yaml")
        wd = ctx.runs_dir / run_id
        limits = [
            {"name": "max_iterations", "value": body["limits"]["max_iterations"],
             "enforced_by": "engine"},
            {"name": "stall_iterations", "value": sem.get("stall_iterations", 2),
             "enforced_by": "engine"},
            {"name": "failure_breaker_identical_failures", "value": 3,
             "enforced_by": "engine"},
        ]
        if isinstance(sem.get("task_limits"), dict):
            limits.append({"name": "task_limits.max_runs",
                           "value": sem["task_limits"].get("max_runs"),
                           "enforced_by": "advisory",
                           "note": "a validation error at the pin, not a stop: the "
                                   "iteration is marked partial"})
        # Values here are digested verbatim (not templated): never the run id.
        defaults = [] if sem.get("run_id") else [
            {"field": "run_id", "value": "<work-id>-<prep-id>",
             "reason": "generated; non-material (names the work dir only)"}]
        for k, v, why in (("stall_iterations", 2, "Nous default (config.py)"),
                          ("min_improvement", 0, "Nous default (config.py)"),
                          ("session_scope", "campaign", "Nous default (config.py)"),
                          ("experiment_commands", "Nous DEFAULT_EXPERIMENT_COMMANDS",
                           "Nous default (hooks.py)")):
            if sem.get(k) is None:
                defaults.append({"field": k, "value": v, "reason": why})
        try:
            free = shutil.disk_usage(next(x for x in [ctx.runs_dir, *ctx.runs_dir.parents]
                                          if x.exists())).free
            free_s = f"{free / 1e9:.1f} GB free under the runs dir"
        except (OSError, StopIteration):
            free_s = "free space under the runs dir: unknown"
        return Prepared(
            material={"engine": "nous",
                      "campaign_keys": list(doc),
                      "apparatus": {"root": p.get("apparatus_root"), **facts},
                      "blis_knobs": list((sem.get("blis") or {}).get("knobs") or [])},
            argv=["nous", "run", campaign, "--run-id", run_id],
            files={"campaign.yaml": self.dump_yaml(doc)},
            cwd=str(ctx.target),
            env_set={ENV_PARENT: str(ctx.runs_dir)},
            secret_vars=tuple(sem.get("secrets") or ()),
            limits=limits,
            defaults=defaults,
            expected_artifacts=[
                {"kind": "nous-work-dir", "location": str(wd)},
                {"kind": "nous-state", "location": str(wd / "state.json")},
                {"kind": "nous-worktree", "location": str(ctx.target / WORKTREE_DIR / run_id)},
            ],
            non_material={"run_id": run_id},
            native_run_id=run_id,
            summary=[f"nous run id: {run_id} (non-material)",
                     f"nous work dir: {wd}",
                     f"nous worktree: {ctx.target / WORKTREE_DIR / run_id} "
                     f"(branch nous/{run_id}, built from HEAD)",
                     "stop: `nous stop` takes effect after the current EXECUTE, then "
                     "REPORT runs (a full model turn)",
                     "cost: reported by Nous, incomplete (advisory only)",
                     free_s],
        )

    def execute(self, record: dict, ctx: AdapterContext) -> LaunchSpec:
        return LaunchSpec(argv=list(record["argv"]), cwd=record["cwd"],
                          env_set=dict(record["env_set"]),
                          native_run_id=record.get("native_run_id"))

    # --- native evidence -----------------------------------------------------

    def evidence(self, invocation: dict, record: dict, ctx: AdapterContext) -> list[dict]:
        out = super().evidence(invocation, record, ctx)
        try:
            wd = self.work_dir(record)
        except (KeyError, TypeError):
            return out
        have = {a["location"] for a in out}
        for kind, name in NATIVE_FILES:
            p = wd / name
            if p.is_file() and str(p) not in have:
                out.append({"kind": kind, "location": str(p)})
        return out

    def classify(self, invocation: dict, record: dict, ctx: AdapterContext) -> NativeState:
        """Plan §1 Q2, rows in order, first match wins. The runtime calls
        this only once the engine process is gone (rows 'running' and
        'recovery_required' are the runtime's, R3)."""
        wd = self.work_dir(record)
        ev = [{"kind": k, "location": str(wd / n)} for k, n in NATIVE_FILES
              if (wd / n).is_file()]
        cancel = invocation.get("cancel_requested_at") is not None
        rc = invocation.get("exit_code")
        rc_s = rc if rc is not None else "unknown"
        state = _read_json(wd / "state.json")
        ledger = _read_json(wd / "ledger.json")
        last = (ledger[-1] if isinstance(ledger, list) and ledger
                and isinstance(ledger[-1], dict) else {})
        stop = last.get("stop_reason")
        has_final = (wd / "final_check.json").is_file()
        warnings = []
        fc = _read_json(wd / "final_check.json") if has_final else None
        if isinstance(fc, dict) and fc.get("answer_valid") is not True:
            warnings.append("final_check.answer_valid is not true; claims need review")

        if (wd / "halt.json").is_file():
            h = _read_json(wd / "halt.json")
            h = h if isinstance(h, dict) else {}
            return NativeState("failed", "halt", partial=True, evidence=ev,
                               warnings=warnings,
                               reason=f"halt: repeated identical failures in phase "
                                      f"{h.get('phase', '?')} ({h.get('message', '')})"
                                      + ("; REPORT ran, findings are partial"
                                         if has_final else ""))
        if has_final and stop in COMPLETED_STOPS:
            return NativeState("completed", stop, evidence=ev, warnings=warnings)
        if has_final and stop == "stop_sentinel":
            if cancel:
                return NativeState("cancelled", stop, partial=True, evidence=ev,
                                   warnings=warnings,
                                   reason="stopped by the runtime's cancel (nous stop); "
                                          "REPORT ran, output may be partial")
            return NativeState("completed", stop, evidence=ev, warnings=warnings + [
                "a STOP sentinel was written outside the runtime (no cancel request "
                "recorded)"])
        if isinstance(state, dict) and state.get("stop_reason") == "no_baseline":
            return NativeState("failed", "no_baseline", reason="no_baseline: no valid "
                               "baseline measurement exists", evidence=ev, warnings=warnings)
        if isinstance(state, dict) and not has_final:
            if cancel and rc == SIGINT_EXIT:
                return NativeState("cancelled", None, partial=True, evidence=ev,
                                   warnings=warnings,
                                   reason="interrupted by the runtime's cancel (SIGINT, "
                                          "exit 130); resumable with a new preparation")
            return NativeState("failed", None, evidence=ev, warnings=warnings,
                               reason=f"engine exited (exit {rc_s}) without final_check."
                                      f"json, phase {state.get('phase')} iteration "
                                      f"{state.get('iteration')}; resumable with a new "
                                      "preparation")
        if has_final:
            return NativeState("failed", stop, evidence=ev, warnings=warnings,
                               reason="unclassified_native_state: final_check.json present "
                                      f"but last ledger stop_reason is {stop!r}")
        return NativeState("failed", None, evidence=ev, warnings=warnings,
                           reason=f"no Nous state.json in {wd} (exit {rc_s}); the engine "
                                  "stopped before creating its work dir (e.g. a campaign "
                                  "or preflight error)")

    # --- cancel --------------------------------------------------------------

    def cancel(self, invocation: dict, record: dict, ctx: AdapterContext, *,
               interrupt: bool = False) -> dict:
        """Plan §4.4: `nous stop` (STOP sentinel; effective after the current
        EXECUTE, then REPORT runs, G6), or with interrupt SIGINT to the engine
        (exit 130, resumable). SIGTERM is never used (no handler at the pin)."""
        if interrupt:
            r = self.interrupt_engine(invocation)
            r["note"] = "SIGINT: Nous exits 130 and the run stays resumable"
            return r
        wd = self.work_dir(record)
        argv = ["nous", "stop", str(wd), "--reason",
                f"ains-runtime cancel {invocation.get('inv_id')}"]
        if not wd.is_dir():
            return {"mechanism": "nous-stop", "requested": False, "argv": argv,
                    "error": f"work dir {wd} does not exist yet; retry cancel, or use "
                             "--interrupt"}
        env = {**{k: v for k, v in ctx.env.items()}, **record.get("env_set", {})}
        rc, outp = self.run_cmd(argv, env)
        return {"mechanism": "nous-stop", "requested": rc == 0, "argv": argv,
                "error": None if rc == 0 else f"nous stop exited {rc}: {outp[-500:]}",
                "note": "takes effect after the current iteration's EXECUTE; REPORT then "
                        "still runs. Reported cancel_requested until terminal."}

    # --- collect -------------------------------------------------------------

    def collect(self, invocation: dict, record: dict, ctx: AdapterContext) -> dict:
        """Read-only normalized collection (plan §1 Q4). Shape:

        {capability, state: collected|unavailable, reason?, work_dir,
         native_run_id, native_state (classify), partial,
         outcome {classification: unavailable, reason, partial},
         hypotheses [{id, iteration, statement, statement_ref, native_status,
                      classification, error_type, findings_ref, partial_note?,
                      partial}],
         claims (collect.py claims_draft), answer_ref, final_check, usable,
         limitations [{source, text, location}], methods, spend,
         bundle (the collect.py bundle, for collection.json)}
        """
        import collect as C
        wd = self.work_dir(record)
        base = {"capability": self.capability, "work_dir": str(wd),
                "native_run_id": record.get("native_run_id")}
        try:
            norm = C.normalize(wd)
        except C.CouldNotRun as e:
            return {**base, "state": "unavailable", "reason": str(e)}
        ns = self.classify(invocation, record, ctx)
        partial = bool(ns.partial)
        hyps = [{**h, "partial": partial} for h in norm["hypotheses"]]
        lims = list(norm["limitations"])
        sem = ((record.get("material") or {}).get("request") or {}).get("semantics") or {}
        for t in ((sem.get("blis") or {}).get("limitations") or []):
            lims.append({"source": "coordinator", "text": t, "location": None})
        if partial:
            lims.append({"source": "runtime", "text": f"findings are partial: {ns.reason}",
                         "location": None})
        b = norm["bundle"]
        return {**base, "state": "collected", "native_state": ns.as_dict(),
                "partial": partial,
                "outcome": {"classification": "unavailable", "partial": partial,
                            "reason": "Nous records no study-level outcome; the "
                                      "coordinator classifies it against the request's "
                                      "hypothesis (spec §11)"},
                "hypotheses": hyps, "claims": b["claims_draft"],
                "answer_ref": b["artifacts"].get("answer"),
                "final_check": b["final_check"], "usable": b["usable"],
                "limitations": lims, "methods": norm["methods"], "spend": norm["spend"],
                "bundle": b}
