"""Spotlights static adapter (plan §5 R5; §1 Q3-Q6; G3, G8, G9, G11; §4.4).

Capability `spotlights-static`: one `spotlights-engine` run over named
modules of the target at the prepared commit. Engine facts come from
references/upstreams/spotlights.json, cited at the version locked in lock.json.

Request (experiment-request body):

  objective              required, non-blank (SpotlightContext.objective)
  limits.max_cost_usd    optional number > 0 -> `--max-cost`. ADVISORY: the
                         engine validates and prints it, never enforces it
                         (spotlights.json max_cost_advisory; G3). Labelled advisory.
  semantics (all material):
    include   [qn, ...]  required, >= 1 slash-form module names, each segment
                         [a-z][a-z0-9_]* (schemas/project.py _NAME_PATTERN)
    models    {claude, codex}  both required: `--claude-model` and
                         `--codex-model` are always passed, so nothing falls
                         back to models.yaml or ~/.claude / ~/.codex (Q5)
    hints     [text]     optional, `--hint` each
    repo_url  text       optional, `--repo-url` (manifest provenance)
    secrets   [ENV]      optional, variables passed to the engine by name
                         (presence recorded only), e.g. OPENAI_API_KEY

Not semantics, and not material: the output folder and artifacts dir. They
are fresh per preparation, `$RUNS/<repo>/<slug>/spotlights-<prep-id>/` and
its `artifacts/`, so an equivalent request gets the same digest (plan §4.2)
and every invocation starts in a directory that does not exist (G9: the
engine's run id is a hash of its inputs and an existing artifacts dir is
resumed by default). `--no-resume` is passed too, so the engine itself
refuses an existing run dir. Resume is not supported yet.

validate: engine found on PATH and its installed git sha == the lock.json pin
(G11, via doctor's direct_url.json reader, not the package version);
objective, models, include present; every include name matches a directory
(or file stem) of the target at the request commit (a path approximation:
module names come from the step-1 LLM extractor, so they can't be known
before the run, and the engine only warns and skips a name that matches
nothing, spotlights.json unmatched_include); output folder and artifacts dir outside the
target and not existing.

prepare: argv (no shell), then a `--dry-run` preflight with the same flags
but `--output-folder`/`--artifacts-dir` in a temp dir that is deleted
afterwards (the dry run mkdirs both, spotlights.json dry_run_mkdirs; it makes no model or
network call, R0 §1.7 item 3). The resolved models it prints are recorded
and must equal the requested ones. The runner is injectable
(`dry_run_runner`); tests never run the engine.

classify (Q4, G8): from result.json + run_manifest.json + the exit code.
  failed     exit != 0 (1 = a recoverable=False issue, 2 = bad models file),
             any FAILED module, any recoverable=False issue, result.json or
             run_manifest.json missing/unreadable, no module analysed, or
             zero candidates alongside DEGRADED modules / manager issues
             (not a clean no_opportunity; never completed by default)
  completed  native_stop_reason no_opportunity (0 candidates, no issues) or
             candidates_found (partial if a module is DEGRADED or
             manager_issues is non-empty)
  cancelled  a cancel was requested and the engine ended without result.json
A lost exit code (supervisor gone) with clean native evidence is classified
from the evidence, with a warning.

cancel (§4.4): no native stop and no signal handlers at the pin. SIGTERM
then SIGKILL to every distinct process group among the engine and its
descendants (step 1 starts `claude` in new sessions), best effort, survivors
listed. `interrupt` has no separate meaning here. Unsupported interruption:
final reports are not written.

collect: the Q4 outcome; for a completed run, collect_spotlights.collect_run
into the study (copies, verbatim code citations, claims_draft) at the
prepared commit, then the estimate guard (no Spotlights estimate is ever a
claim value). Failed and cancelled runs are not collected (no findings).

D5: objective-setting and sort-candidates stay coordinator-side, in-session
steps; sort runs after collection, and collect can be rerun to pick it up.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

from .base import Adapter, AdapterContext, LaunchSpec, NativeState, Prepared

_SCRIPTS = Path(__file__).resolve().parent.parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import upstreams as U  # noqa: E402  (references/upstreams/: the only home of upstream facts)

ENGINE = "spotlights-engine"
PIN_KEY = "spotlights-pin"         # doctor's key; the value comes from lock.json
CAPABILITY = "spotlights-static"

QN_RE = re.compile(r"^[a-z][a-z0-9_]*(/[a-z][a-z0-9_]*)*$")
CODEX_MODEL_RE = re.compile(U.fact("spotlights", "codex_model_re"))
CLAUDE_MODEL_RE = re.compile(U.fact("spotlights", "claude_model_re"))
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SEG_BAD = re.compile(U.fact("spotlights", "module_segment_invalid_chars"))

# Q5: every SPOTLIGHTS_* name under src/ at the lock. Each names a file; the
# value and the file's digest are material.
FILE_VARS = tuple(U.fact("spotlights", "file_vars"))
MATERIAL_VARS = tuple(U.fact("spotlights", "material_vars"))

DRY_RUN_TIMEOUT_S = 120
# The native outcomes (spotlights.json outcomes): the engine's own stop
# reasons plus the runtime's failed/cancelled. classify() emits these and
# results.spotlights_static() maps them, so both compare against the fact —
# a lock bump renaming one must fail here, not degrade silently.
OUTCOMES = tuple(U.fact("spotlights", "outcomes"))
CANDIDATES_FOUND, NO_OPPORTUNITY = OUTCOMES[0], OUTCOMES[1]
BAD_MODELS_EXIT = U.fact("spotlights", "bad_models_exit")
_DRY_FIELDS = "|".join(re.escape(f) for f in U.fact("spotlights", "dry_run_fields"))

DryRunner = Callable[[list, str, dict], "tuple[int, str, str]"]
EngineProbe = Callable[[AdapterContext], dict]


class SpotlightsStaticError(Exception):
    pass


def normalize_segment(raw: str) -> str:
    """_normalize_module_segment at the lock (spotlights.json)."""
    s = _SEG_BAD.sub("_", raw.strip().lower())
    return s if s and s[0].isalpha() else "m_" + s


def _default_probe(ctx: AdapterContext) -> dict:
    """doctor's engine record (found, source, path, sha, pin). Reads files
    only (direct_url.json); never runs the engine, no network (offline)."""
    import _config
    import doctor
    try:
        machine = _config.load_machine_config()
    except Exception:
        machine = {}
    e = doctor.engine_info(ENGINE, machine, doctor.read_pins(), True, {})
    return {k: e.get(k) for k in ("found", "source", "path", "sha", "pin")}


def _default_dry_run(argv: list, cwd: str, env: dict) -> tuple[int, str, str]:
    r = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                       timeout=DRY_RUN_TIMEOUT_S)
    return r.returncode, r.stdout, r.stderr


def parse_dry_run(stdout: str) -> dict:
    """The fields the locked dry run prints (spotlights.json dry_run_fields)."""
    out = {}
    for line in stdout.splitlines():
        m = re.match(rf"^\s+({_DRY_FIELDS}):\s*(.*?)\s*$", line)
        if m:
            out[m.group(1).replace("-", "_")] = m.group(2)
    return out


def _load(path: Path):
    import json
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class SpotlightsStaticAdapter(Adapter):
    capability = CAPABILITY
    version = "spotlights-static-0.1"
    MATERIAL_FIELDS = ("include", "models", "hints", "repo_url", "secrets")
    NON_MATERIAL_FIELDS: Mapping[str, str] = {}
    # Not semantics keys but non-material values (Prepared.non_material):
    # output_folder / artifacts_dir, fresh per preparation (plan §4.2, G9).

    def __init__(self, *, engine_probe: EngineProbe | None = None,
                 dry_run_runner: DryRunner | None = None, cancel_grace: float = 10.0):
        self.engine_probe = engine_probe or _default_probe
        self.dry_run_runner = dry_run_runner or _default_dry_run
        self.cancel_grace = cancel_grace

    # --- paths ----------------------------------------------------------------

    @staticmethod
    def run_name(ctx: AdapterContext) -> str:
        # prep ids are "prep-" + 12 hex: long and unique, so whole-token
        # templating can't hit an unrelated path.
        return f"spotlights-{ctx.prep_dir.name}"

    def paths(self, ctx: AdapterContext) -> tuple[Path, Path]:
        out = Path(ctx.runs_dir) / self.run_name(ctx)
        return out, out / "artifacts"

    @staticmethod
    def _record_paths(record: dict) -> tuple[Path, Path]:
        nm = record.get("non_material", {})
        return Path(nm["output_folder"]), Path(nm["artifacts_dir"])

    # --- versions ---------------------------------------------------------------

    def _probe(self, ctx) -> dict:
        try:
            return dict(self.engine_probe(ctx) or {})
        except Exception as e:  # engine missing or unreadable must not crash prepare
            return {"found": False, "error": str(e)}

    def versions(self, ctx: AdapterContext) -> dict:
        p = self._probe(ctx)
        if not p.get("found"):
            raise SpotlightsStaticError(f"{ENGINE} not found")
        return {"engine_sha": p.get("sha")}

    # --- validate ----------------------------------------------------------------

    def _semantic_problems(self, body: dict) -> list[str]:
        out = []
        sem = body.get("semantics") or {}
        if not str(body.get("objective") or "").strip():
            out.append("objective: required (non-blank) for spotlights-static")
        models = sem.get("models")
        if not isinstance(models, dict):
            out.append("semantics.models: required {claude, codex} (no implicit "
                       "default: models.yaml and ~/.claude / ~/.codex are never used)")
        else:
            for k in sorted(set(models) - {"claude", "codex"}):
                out.append(f"semantics.models.{k}: unknown (only claude, codex)")
            for k, rx in (("claude", CLAUDE_MODEL_RE), ("codex", CODEX_MODEL_RE)):
                v = models.get(k)
                if not isinstance(v, str) or not v.strip():
                    out.append(f"semantics.models.{k}: required (passed as --{k}-model)")
                elif not rx.match(v):
                    out.append(f"semantics.models.{k}: {v!r} is not a valid model id")
        inc = sem.get("include")
        if not isinstance(inc, list) or not inc:
            out.append("semantics.include: required, one or more module names "
                       "(e.g. [\"v1/kv_offload\"]); a whole-repo run is not prepared")
        else:
            for q in inc:
                if not isinstance(q, str) or not QN_RE.match(q):
                    hint = ""
                    if isinstance(q, str) and q.strip("/"):
                        hint = " (normalized: " + "/".join(
                            normalize_segment(s) for s in q.strip("/").split("/") if s) + ")"
                    out.append(f"semantics.include: {q!r} is not a slash-form module "
                               f"name of [a-z][a-z0-9_]* segments{hint}")
            if len(set(map(str, inc))) != len(inc):
                out.append("semantics.include: duplicate names")
        hints = sem.get("hints")
        if hints is not None and not (isinstance(hints, list) and all(
                isinstance(h, str) and h.strip() for h in hints)):
            out.append("semantics.hints: a list of non-blank strings")
        url = sem.get("repo_url")
        if url is not None and not (isinstance(url, str) and url.strip()):
            out.append("semantics.repo_url: a non-blank string")
        secrets = sem.get("secrets")
        if secrets is not None and not (isinstance(secrets, list) and all(
                isinstance(s, str) and ENV_NAME_RE.match(s) for s in secrets)):
            out.append("semantics.secrets: a list of environment variable names")
        mc = (body.get("limits") or {}).get("max_cost_usd")
        if mc is not None and (isinstance(mc, bool) or not isinstance(mc, (int, float))
                               or mc <= 0):
            out.append("limits.max_cost_usd: a number > 0 (advisory; the engine "
                       "never enforces it)")
        return out

    def _engine_problems(self, ctx) -> list[str]:
        import doctor
        p = self._probe(ctx)
        if not p.get("found"):
            return [f"{ENGINE} not found (not on PATH and no engines.{ENGINE} in the "
                    f"machine config){': ' + p['error'] if p.get('error') else ''}"]
        out = []
        if p.get("source") != "PATH" or not p.get("path"):
            out.append(f"{ENGINE} is configured as a checkout ({p.get('path')}); the "
                       "runtime launches only the installed executable on PATH")
        pin = p.get("pin") or doctor.read_pins().get(PIN_KEY)
        _, pin_sha = doctor.split_pin(pin)
        if not pin_sha:
            out.append(f"no {PIN_KEY} sha in references/upstreams/lock.json to compare the "
                       "engine with")
        elif not p.get("sha"):
            out.append(f"can't read the installed {ENGINE} git sha (direct_url.json) "
                       f"to compare with the pin {pin}")
        elif not doctor.same_sha(p["sha"], pin_sha):
            out.append(f"{ENGINE} sha {p['sha'][:12]} is not the pin {pin} (G11: the git "
                       "sha is compared, not the package version)")
        return out

    @staticmethod
    def module_paths_at(target: Path, commit: str) -> set[str] | None:
        """Normalized qualified-name forms of every directory and file stem
        in the tree at commit (None if git can't list it)."""
        import _config
        out = _config.git("ls-tree", "-r", "-t", "--name-only", commit, cwd=target)
        if out is None:
            return None
        names = set()
        for path in out.splitlines():
            parts = [s for s in path.split("/") if s]
            if not parts:
                continue
            parts[-1] = parts[-1].rsplit(".", 1)[0] or parts[-1]
            norm = [normalize_segment(s) for s in parts]
            for i in range(len(norm)):          # every suffix: unknown source_root
                names.add("/".join(norm[i:]))
        return names

    def _include_problems(self, request: dict, ctx) -> list[str]:
        inc = (request["body"].get("semantics") or {}).get("include")
        if not isinstance(inc, list) or not all(isinstance(q, str) and QN_RE.match(q)
                                                for q in inc):
            return []          # already reported
        commit = request["target"]["commit"]
        names = self.module_paths_at(Path(ctx.target), commit)
        if names is None:
            return [f"can't list the target tree at {commit} to check --include"]
        return [f"semantics.include: {q!r} matches no directory or file at {commit[:12]} "
                "(the engine would warn and skip it)" for q in inc if q not in names]

    def _dir_problems(self, ctx) -> list[str]:
        out = []
        target = Path(ctx.target).resolve()
        for label, p in zip(("output folder", "artifacts dir"), self.paths(ctx)):
            r = p.resolve()
            if r == target or target in r.parents:
                out.append(f"{label} {p} is inside the target repo (G9; the engine also "
                           "refuses an artifacts dir inside the repo)")
            if p.exists():
                out.append(f"{label} {p} already exists; every invocation needs a "
                           "fresh one (G9: an existing artifacts dir is resumed)")
        return out

    def validate(self, request: dict, ctx: AdapterContext) -> list[str]:
        body = request["body"]
        return (self._semantic_problems(body) + self._engine_problems(ctx)
                + self._include_problems(request, ctx) + self._dir_problems(ctx))

    # --- prepare -------------------------------------------------------------------

    def build_argv(self, exe: str, request: dict, ctx: AdapterContext,
                   output: Path, artifacts: Path) -> list[str]:
        body = request["body"]
        sem = body["semantics"]
        argv = [exe, "--repo", str(ctx.target), "--include", *sem["include"],
                f"--objective={body['objective']}"]
        argv += [f"--hint={h}" for h in sem.get("hints") or []]
        if sem.get("repo_url"):
            argv.append(f"--repo-url={sem['repo_url']}")
        argv += [f"--claude-model={sem['models']['claude']}",
                 f"--codex-model={sem['models']['codex']}"]
        mc = (body.get("limits") or {}).get("max_cost_usd")
        if mc is not None:
            argv.append(f"--max-cost={float(mc)!r}")
        argv += ["--output-folder", str(output), "--artifacts-dir", str(artifacts),
                 "--no-resume"]
        return argv

    def _dry_env(self, ctx, sem) -> dict:
        from ..envpolicy import OBSERVED_PREFIXES
        from ..supervisor import BASE_ENV_VARS
        names = set(BASE_ENV_VARS) | set(FILE_VARS) | set(MATERIAL_VARS) | set(
            sem.get("secrets") or ())
        env = {n: v for n, v in ctx.env.items()
               if n in names or n.startswith(tuple(OBSERVED_PREFIXES))}
        return env

    def dry_run(self, argv: list[str], request: dict, ctx: AdapterContext) -> dict:
        """--dry-run with the same flags, output dirs in a deleted temp dir."""
        from ..envpolicy import redact
        from ..preparation import PrepareError
        out, art = self.paths(ctx)
        with tempfile.TemporaryDirectory(prefix="ains-spotlights-dry-") as td:
            sub = {str(out): str(Path(td) / "output"), str(art): str(Path(td) / "artifacts")}
            dry = [sub.get(a, a) for a in argv if a != "--no-resume"] + ["--dry-run"]
            try:
                rc, stdout, stderr = self.dry_run_runner(dry, str(ctx.target),
                                                         self._dry_env(ctx, request["body"]
                                                                       ["semantics"]))
            except (OSError, subprocess.SubprocessError) as e:
                raise PrepareError(f"{ENGINE} --dry-run could not run: {e}") from e
        if rc != 0:
            tail = redact((stderr or stdout or "").strip()[-400:], ctx.env)
            raise PrepareError(f"{ENGINE} --dry-run failed (exit {rc}): {tail}")
        got = parse_dry_run(stdout or "")
        models = request["body"]["semantics"]["models"]
        bad = []
        if got.get("claude_model") != models["claude"]:
            bad.append(f"claude {got.get('claude_model')!r} != requested {models['claude']!r}")
        if (got.get("codex_model") or "").split(" ")[0] != models["codex"]:
            bad.append(f"codex {got.get('codex_model')!r} != requested {models['codex']!r}")
        if bad:
            raise PrepareError(f"{ENGINE} --dry-run resolved other models: " + "; ".join(bad))
        return {"claude_model": got.get("claude_model"), "codex_model": got.get("codex_model"),
                "scope": got.get("scope")}

    def material_files(self, ctx) -> dict[str, str]:
        home = ctx.env.get("HOME") or ""
        codex_home = ctx.env.get("CODEX_HOME") or (os.path.join(home, ".codex") if home else "")
        out = {}
        if home:
            out["claude settings (~/.claude/settings.json)"] = os.path.join(
                home, ".claude", "settings.json")
        if codex_home:
            out["codex config (config.toml)"] = os.path.join(codex_home, "config.toml")
        return out

    def prepare(self, request: dict, ctx: AdapterContext) -> Prepared:
        from ..limits import ADVISORY
        body = request["body"]
        sem = body["semantics"]
        probe = self._probe(ctx)
        exe = probe.get("path") or ENGINE
        out, art = self.paths(ctx)
        argv = self.build_argv(exe, request, ctx, out, art)
        resolved = self.dry_run(argv, request, ctx)
        limits = [{"name": "fresh_artifacts_dir", "value": True, "enforced_by": "runtime"},
                  {"name": "resume", "value": "never (--no-resume)", "enforced_by": "engine"}]
        mc = (body.get("limits") or {}).get("max_cost_usd")
        if mc is not None:
            limits.append({"name": "max_cost_usd", "value": mc, "enforced_by": ADVISORY,
                           "note": "spotlights-engine --max-cost is advisory at the pin: "
                                   "validated and printed, never enforced (G3)"})
        return Prepared(
            material={"engine": ENGINE, "dry_run": resolved,
                      "include": list(sem["include"])},
            argv=argv,
            cwd=str(ctx.target),
            material_vars=MATERIAL_VARS,
            file_vars=FILE_VARS,
            material_files=self.material_files(ctx),
            secret_vars=tuple(sem.get("secrets") or ()),
            limits=limits,
            defaults=[
                {"field": "deep_research", "value": "enabled",
                 "reason": "engine default (no --no-deep-research)"},
                {"field": "review_iterations", "value": 3,
                 "reason": "engine default (DiscoveryConfig)"},
                {"field": "max_parallel", "value": 1, "reason": "engine default"},
            ],
            expected_artifacts=[
                {"kind": "spotlights-artifacts-dir", "location": str(art)},
                {"kind": "spotlights-result", "location": str(out / "result.json")},
                {"kind": "spotlights-run-manifest", "location": str(out / "run_manifest.json")},
            ],
            non_material={"artifacts_dir": str(art), "output_folder": str(out)},
            native_run_id=self.run_name(ctx),
            summary=[
                f"dry run: claude {resolved['claude_model']}, codex "
                f"{resolved['codex_model']}, scope {resolved['scope']}",
                "--max-cost is advisory: spend is reported, never capped",
                "no native stop: cancel signals every process group (best effort); "
                "a cancelled run writes no final report",
                "agents run in the live target checkout (clean at prepare and launch)",
                "objective-setting and sort-candidates stay in-session (D5)",
            ],
        )

    def execute(self, record: dict, ctx: AdapterContext) -> LaunchSpec:
        argv = list(record["argv"])
        if "--dry-run" in argv or "--no-resume" not in argv:
            raise SpotlightsStaticError("prepared argv is not a fresh real run")
        return LaunchSpec(argv=argv, cwd=record["cwd"], env_set=dict(record["env_set"]),
                          native_run_id=record.get("native_run_id"))

    # --- native evidence (Q4) ---------------------------------------------------------

    def assess(self, invocation: dict, record: dict) -> dict:
        """Q4 classification from native evidence. Read-only."""
        out_dir, art = self._record_paths(record)
        rc = invocation.get("exit_code")
        cancel = invocation.get("cancel_requested_at") is not None
        result_p, manifest_p = out_dir / "result.json", out_dir / "run_manifest.json"
        if not manifest_p.is_file() and (art / "spotlights_manager" / "run_manifest.json").is_file():
            manifest_p = art / "spotlights_manager" / "run_manifest.json"
        evidence = [{"kind": k, "location": str(p)} for k, p in (
            ("spotlights-result", result_p), ("spotlights-run-manifest", manifest_p),
            ("spotlights-index", out_dir / "index.md"),
            ("spotlights-artifacts-dir", art)) if p.exists()]
        a: dict[str, Any] = {"state": "failed", "outcome": "failed", "reason": None,
                             "partial": False, "warnings": [], "evidence": evidence,
                             "exit_code": rc, "n_candidates": None, "module_status": {},
                             "issues": [], "result": None, "manifest": None,
                             "output_folder": str(out_dir), "artifacts_dir": str(art)}
        result = _load(result_p) if result_p.is_file() else None
        manifest = _load(manifest_p) if manifest_p.is_file() else None
        a["result"], a["manifest"] = result, manifest

        def fail(reason):
            a.update(state="failed", outcome="failed", reason=reason)
            return a

        if cancel and result is None:
            a.update(state="cancelled", outcome="cancelled", partial=bool(evidence),
                     reason=f"stopped after a cancel request (exit "
                            f"{rc if rc is not None else 'unknown'}); unsupported "
                            "interruption: no final report, stages left in progress")
            return a
        if cancel:
            a["warnings"].append("a cancel was requested but the engine finished first; "
                                 "reporting its real terminal state")
        if rc is not None and rc < 0 and result is None:
            return fail(f"killed by signal {-rc} with no cancel request")
        if rc == BAD_MODELS_EXIT:
            return fail(f"exit {rc}: bad models file or model id (spotlights.json bad_models_exit)")
        if not isinstance(result, dict):
            return fail("result.json missing or unreadable"
                        + (f" (exit {rc})" if rc is not None else ""))
        if not isinstance(manifest, dict):
            return fail("run_manifest.json missing or unreadable")
        runs = result.get("module_runs") if isinstance(result.get("module_runs"), dict) else {}
        status: dict[str, str] = {}
        issues = []
        for qn, mr in sorted(runs.items()):
            mr = mr if isinstance(mr, dict) else {}
            status[qn] = str(mr.get("status"))
            for i in mr.get("issues") or []:
                if isinstance(i, dict):
                    issues.append({"source": "module", "module": qn, **{
                        k: i.get(k) for k in ("step", "severity", "message", "recoverable")}})
        mgr = [i for i in result.get("manager_issues") or [] if isinstance(i, dict)]
        issues += [{"source": "manager", "module": None, **{
            k: i.get(k) for k in ("step", "severity", "message", "recoverable")}} for i in mgr]
        a["module_status"], a["issues"] = status, issues
        outputs = manifest.get("outputs") or {}
        n = outputs.get("num_candidates")
        report = result.get("report") if isinstance(result.get("report"), dict) else {}
        n_report = len([c for c in report.get("candidates") or [] if isinstance(c, dict)])
        if not isinstance(n, int) or isinstance(n, bool):
            n = n_report
            a["warnings"].append("run_manifest outputs.num_candidates missing; counted "
                                 "result.json candidates")
        elif n != n_report:
            a["warnings"].append(f"run_manifest says {n} candidates, result.json has "
                                 f"{n_report}")
        a["n_candidates"] = n
        if rc is not None and rc != 0:
            return fail(f"exit {rc}" + (" (a recoverable=False issue)" if rc == 1 else ""))
        if any(i.get("recoverable") is False for i in issues):
            return fail("a recoverable=False issue was reported")
        failed_mods = [q for q, s in status.items() if s == "FAILED"]
        if failed_mods:
            return fail(f"module(s) FAILED: {', '.join(failed_mods)}")
        if not any(s in ("SUCCEEDED", "DEGRADED") for s in status.values()):
            return fail("no module was analysed (include matched nothing, or every "
                        "module was SKIPPED)")
        degraded = [q for q, s in status.items() if s == "DEGRADED"]
        if rc is None:
            a["warnings"].append("exit code unknown (supervisor lost); classified from "
                                 "result.json and run_manifest.json")
        if n == 0:
            if degraded or mgr:
                return fail("unclassified_native_state: zero candidates with DEGRADED "
                            "modules or manager issues is not a clean no_opportunity")
            a.update(state="completed", outcome=NO_OPPORTUNITY)
            return a
        a.update(state="completed", outcome=CANDIDATES_FOUND,
                 partial=bool(degraded or mgr))
        if a["partial"]:
            a["reason"] = ("partial: " + ", ".join(
                ([f"DEGRADED {', '.join(degraded)}"] if degraded else [])
                + ([f"{len(mgr)} manager issue(s)"] if mgr else [])))
        return a

    def classify(self, invocation: dict, record: dict, ctx: AdapterContext) -> NativeState:
        a = self.assess(invocation, record)
        commit = (record.get("material") or {}).get("target", {}).get("commit")
        got = ((a["manifest"] or {}).get("target") or {}).get("commit_sha")
        if a["manifest"] and commit and got and got != commit:
            a["warnings"].append(f"run_manifest target.commit_sha {got} is not the "
                                 f"prepared commit {commit}")
        stop = a["outcome"] if a["state"] in ("completed", "cancelled") else None
        return NativeState(a["state"], stop, reason=a["reason"], partial=a["partial"],
                           evidence=a["evidence"], warnings=a["warnings"])

    def evidence(self, invocation: dict, record: dict, ctx: AdapterContext) -> list[dict]:
        return Adapter.evidence(self, invocation, record, ctx)

    # --- cancel --------------------------------------------------------------------

    def cancel(self, invocation: dict, record: dict, ctx: AdapterContext, *,
               interrupt: bool = False) -> dict:
        res = dict(self.stop_process_tree(invocation, grace=self.cancel_grace))
        res["note"] = ("spotlights-engine has no stop mechanism: every process group of "
                       "the engine and its descendants was signalled (best effort). "
                       "Unsupported interruption: no final report is written.")
        if interrupt:
            res["note"] += " --interrupt has no separate meaning for this capability."
        return res

    # --- collect ---------------------------------------------------------------------

    @staticmethod
    def spend(manifest) -> dict:
        from ..limits import spend_unknown
        if not isinstance(manifest, dict) or not isinstance(manifest.get("cost"), dict):
            return spend_unknown("run_manifest.json cost (missing)")
        total, complete = 0.0, True
        for key in ("cost", "external_cost"):
            c = manifest.get(key)
            if not isinstance(c, dict):
                continue
            amt = c.get("amount_usd")
            if isinstance(amt, (int, float)) and not isinstance(amt, bool):
                total += float(amt)
            share = c.get("priced_token_share")
            if not isinstance(share, (int, float)) or share < 1.0:
                complete = False
        return {"reported_usd": round(total, 6),
                "completeness": "complete" if complete else "partial",
                "source": "run_manifest.json cost + external_cost (priced_token_share)"}

    @staticmethod
    def limitations(a: dict) -> list[dict]:
        out = []
        for q, s in sorted(a["module_status"].items()):
            if s in ("DEGRADED", "SKIPPED"):
                out.append({"source": "spotlights", "text": f"module {q} {s}"})
        for i in a["issues"]:
            out.append({"source": "spotlights",
                        "text": f"{i['source']} issue{' in ' + i['module'] if i['module'] else ''}"
                                f" [{i.get('step')}/{i.get('severity')}"
                                f"{'' if i.get('recoverable') is not False else ', not recoverable'}"
                                f"]: {i.get('message')}"})
        out.append({"source": "adapter",
                    "text": "Spotlights candidates, estimated impact, proposals and sort "
                            "scores are model estimates; only verbatim code citations are "
                            "evidence"})
        return out

    def collect(self, invocation: dict, record: dict, ctx: AdapterContext) -> dict:
        """See module docstring. `ctx.study` (optional attribute) overrides the
        study dir, default <target>/investigations/<slug of runs_dir>."""
        import collect_spotlights as CS
        import _collection
        a = self.assess(invocation, record)
        commit = (record.get("material") or {}).get("target", {}).get("commit")
        ns = self.classify(invocation, record, ctx)
        res: dict[str, Any] = {
            "capability": CAPABILITY, "adapter_version": self.version,
            "outcome": a["outcome"], "partial": a["partial"], "reason": a["reason"],
            "native": ns.as_dict(), "exit_code": a["exit_code"],
            "n_candidates": a["n_candidates"], "module_status": a["module_status"],
            "issues": a["issues"], "evidence": a["evidence"],
            "limitations": self.limitations(a) if a["result"] else [],
            "spend": self.spend(a["manifest"]),
            "versions": {"engine_sha": (record.get("material") or {}).get(
                "versions", {}).get("engine_sha"),
                "manifest_spotlights_commit": ((a["manifest"] or {}).get("spotlights")
                                               or {}).get("commit_sha")},
            "collection": {"state": "not_run", "path": None, "error": None},
            "claims_draft": [], "estimate_guard": {"checked": 0, "violations": []},
            "notes": [],
        }
        if a["state"] != "completed":
            res["notes"].append(f"{a['outcome']}: not collected (failure and cancelled "
                                "results carry no findings); native output kept")
            return res
        study = getattr(ctx, "study", None) or (
            Path(ctx.target) / "investigations" / Path(ctx.runs_dir).name)
        try:
            section, path = CS.collect_run(study, a["output_folder"], repo=ctx.target,
                                           sha=commit)
        except (CS.CouldNotRun, _collection.CollectionError) as e:
            res["collection"] = {"state": "failed", "path": None, "error": str(e)}
            return res
        report = CS.report_of(a["result"])
        sorted_js = CS.find_sorted(Path(a["output_folder"]), "sorted_candidates.json")
        forbidden = CS.estimate_values(report, _load(sorted_js) if sorted_js else None,
                                       a["manifest"])
        claims = section.get("claims_draft") or []
        bad = [c["id"] for c in claims if str(c.get("value")) in forbidden]
        res["estimate_guard"] = {"checked": len(claims), "violations": bad}
        if bad:
            res["collection"] = {"state": "failed", "path": str(path),
                                 "error": "a Spotlights estimate became a claim value: "
                                          + ", ".join(bad)}
            return res
        res["collection"] = {"state": "collected", "path": str(path),
                             "section": "spotlights", "error": None}
        res["claims_draft"] = claims
        res["candidates_top"] = section.get("candidates_top", [])
        res["notes"] += list(section.get("notes") or [])
        if a["outcome"] == NO_OPPORTUNITY:
            res["notes"].append(f"{NO_OPPORTUNITY}: the run completed and found no "
                                "candidate in the included modules")
        return res
