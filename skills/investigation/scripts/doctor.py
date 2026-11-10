#!/usr/bin/env python3
"""Preflight for the investigation skill: tools, configs, engines, available methods.

Read-only. Stdlib only: imports fine without PyYAML and reports it missing.

    python3 doctor.py            # human-readable report
    python3 doctor.py --json     # machine-readable, for the agent
    python3 doctor.py --offline  # skip the `git ls-remote` latest-release checks

Exit 0 always, unless the current directory is not inside a git repo (exit 2).

The core output is `methods`: {name: {available: bool, reason?: str}} for
code-reading, telemetry-snapshot, spotlights-static, spotlights-telemetry,
nous-blis and sim2real-prep. A method plan may only pick available methods.

`runtime` reports the AINS local runtime (scripts/runtime.py and its
package): version, store and message schema versions (read from the
package's __init__.py as text; doctor never imports the package),
the SKILL.md pin, and the process start-time method. nous-blis and
spotlights-static launch only through the runtime, so they are unavailable
when it is.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # an install inside a target repo must not dirty it (D4)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _config  # noqa: E402

DOCTOR_VERSION = 1
SKILL_DIR = Path(__file__).resolve().parent.parent
METHODS = ("code-reading", "telemetry-snapshot", "spotlights-static",
           "spotlights-telemetry", "nous-blis", "sim2real-prep")

# name -> (executable on PATH, python dist name for PATH installs, pin key; pins come
# from references/upstreams/lock.json via read_pins)
ENGINES = {
    "nous": ("nous", "nous", "nous-pin"),
    "spotlights-engine": ("spotlights-engine", "spotlights_engine", "spotlights-pin"),
    "signal-pipeline": ("signal-pipeline", "spotlights_engine", "spotlights-pin"),
    "sim2real": ("sim2real", None, None),
}

SPOTLIGHTS_TELEMETRY_REASON = (
    "signal-pipeline can't read a metrics-only capture: its stage 01 needs "
    "traces.jsonl (raw OTel) or a pre-cooked 01_signals.json, and this skill "
    "captures metrics only. Anomaly detection is spotlights' job, so the skill "
    "won't fake either file. Usable only with an `import` capture that already "
    "contains traces.jsonl or 01_signals.json.")

LS_REMOTE_TIMEOUT = 10

# The package name is spelled in two parts on purpose: test_runtime_cli checks
# that its literal name never appears in this file (doctor doesn't import it).
RUNTIME_PKG = SKILL_DIR / "scripts" / ("ains" + "_runtime")
RUNTIME_CLI = SKILL_DIR / "scripts" / "runtime.py"
RUNTIME_CONSTS = ("RUNTIME_VERSION", "STORE_SCHEMA_VERSION", "SCHEMA_VERSIONS",
                  "EXPORT_SCHEMA")
# Methods that launch only through scripts/runtime.py. spotlights-telemetry
# stays on its legacy path for now (owner decision D10).
RUNTIME_GOVERNED = ("nous-blis", "spotlights-static")


# --- small helpers ----------------------------------------------------------

def run(cmd: list[str], cwd=None, timeout=15, env=None) -> str | None:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def read_pins(skill_md: Path = SKILL_DIR / "SKILL.md") -> dict:
    """The engine pins, from references/upstreams/lock.json (the only place
    upstream versions live), as {nous-pin, spotlights-pin: "<ref>@<sha7>"},
    plus the skill's own `metadata:` keys from SKILL.md (version,
    runtime-version). A pin key left in SKILL.md is ignored: lock.json wins."""
    pins = _frontmatter_meta(skill_md)
    for key in [k for k in pins if k.endswith("-pin")]:
        del pins[key]
    try:
        import upstreams
        for name in ("nous", "spotlights"):
            pins[f"{name}-pin"] = upstreams.pin(name)
    except Exception:  # an unreadable lock: engine checks then report "no pin"
        pass
    return pins


def _frontmatter_meta(skill_md: Path) -> dict:
    """Pull `metadata:` keys out of SKILL.md frontmatter with plain line parsing."""
    pins: dict[str, str] = {}
    try:
        lines = skill_md.read_text(encoding="utf-8").splitlines()
    except OSError:
        return pins
    if not lines or lines[0].strip() != "---":
        return pins
    in_meta = False
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not line.startswith((" ", "\t")):
            in_meta = line.rstrip() == "metadata:"
            continue
        if in_meta:
            m = re.match(r"\s+([A-Za-z0-9_.-]+):\s*(.*?)\s*$", line)
            if m:
                pins[m.group(1)] = m.group(2).strip("\"'")
    return pins


def split_pin(pin: str | None) -> tuple[str | None, str | None]:
    if not pin:
        return None, None
    ref, _, sha = pin.partition("@")
    return (ref or None), (sha or None)


def version_key(tag: str):
    return tuple(int(x) for x in re.findall(r"\d+", tag))


def latest_release(url: str) -> dict | None:
    """Highest vX.Y.Z tag on the remote, or None when unreachable (never fatal)."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0",
               GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=5")
    out = run(["git", "ls-remote", "--tags", url], timeout=LS_REMOTE_TIMEOUT, env=env)
    if out is None:
        return None
    tags: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 2 or not parts[1].startswith("refs/tags/"):
            continue
        sha, ref = parts
        name = ref[len("refs/tags/"):]
        peeled = name.endswith("^{}")
        name = name[:-3] if peeled else name
        if not re.fullmatch(r"v?\d+(\.\d+)*", name):
            continue
        if peeled or name not in tags:
            tags[name] = sha
    if not tags:
        return {"tag": None, "sha": None}
    best = max(tags, key=version_key)
    return {"tag": best, "sha": tags[best]}


def same_sha(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    n = min(len(a), len(b))
    return n >= 7 and a[:n] == b[:n]


# --- repo / remotes ---------------------------------------------------------

def repo_info(top: Path, repo_cfg: dict) -> dict:
    remotes: dict[str, str] = {}
    for line in (run(["git", "remote", "-v"], cwd=top) or "").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "(fetch)":
            remotes[parts[0]] = parts[1]
    branch = run(["git", "branch", "--show-current"], cwd=top) or None
    head = run(["git", "rev-parse", "HEAD"], cwd=top)

    configured = repo_cfg.get("upstream_remote") if isinstance(repo_cfg, dict) else None
    if configured:
        upstream, why = str(configured), "upstream_remote in the repo config"
        if upstream not in remotes:
            why += f" (warning: no remote named {upstream!r})"
    elif "upstream" in remotes:
        upstream, why = "upstream", "a remote named 'upstream'"
    elif list(remotes) == ["origin"]:
        upstream, why = "origin", "origin is the only remote"
    else:
        upstream = None
        why = ("can't tell: no remote named 'upstream' and origin isn't the only "
               "remote; set upstream_remote in the repo config") if remotes else "no remotes"
    forks = [r for r in remotes if r != upstream]

    push = None
    if branch:
        push = (run(["git", "config", f"branch.{branch}.pushRemote"], cwd=top)
                or run(["git", "config", "remote.pushDefault"], cwd=top)
                or run(["git", "config", f"branch.{branch}.remote"], cwd=top))
    if not push and "origin" in remotes:
        push = "origin"
    return {"toplevel": str(top), "name": _config.repo_name(top), "branch": branch,
            "head": head, "remotes": remotes, "upstream_remote": upstream,
            "upstream_reason": why, "fork_remotes": forks, "push_remote": push,
            "push_is_upstream": (push == upstream) if (push and upstream) else None}


# --- config layers ----------------------------------------------------------

def layer(path: Path, location: str) -> tuple[dict, dict]:
    info = {"path": str(path), "location": location, "present": path.is_file(),
            "parseable": None, "keys": [], "error": None}
    data: dict = {}
    if info["present"]:
        try:
            data = _config._load_mapping(path)
            info["parseable"] = True
            info["keys"] = sorted(data)
        except _config.ConfigError as e:
            info["parseable"] = False
            info["error"] = str(e)
    return info, data


def config_info(top: Path) -> tuple[dict, dict, dict]:
    m_info, machine = layer(_config.machine_config_path(), "machine")
    committed, home = _config.repo_config_paths(top)
    if committed.is_file():
        r_info, repo = layer(committed, "committed")
    elif home.is_file():
        r_info, repo = layer(home, "home")
    else:
        r_info, repo = layer(committed, "none")
        r_info["searched"] = [str(committed), str(home)]
    r_info["tracking"] = repo.get("tracking")
    if committed.is_file() and home.is_file():
        r_info["shadowed"] = str(home)
    return {"machine": m_info, "repo": r_info}, machine, repo


# --- engines ----------------------------------------------------------------

def checkout_info(path: Path) -> dict:
    info: dict = {"path": str(path)}
    info["sha"] = run(["git", "rev-parse", "HEAD"], cwd=path)
    info["version"] = run(["git", "describe", "--tags", "--always"], cwd=path)
    info["ref"] = run(["git", "branch", "--show-current"], cwd=path) or None
    info["upstream_url"] = run(["git", "remote", "get-url", "origin"], cwd=path)
    pushed = run(["git", "branch", "-r", "--contains", "HEAD"], cwd=path)
    info["pushed"] = bool(pushed) if pushed is not None else None
    return info


def installed_info(exe: str, dist: str | None) -> dict:
    """Version of a PATH-installed python tool, read from its venv's dist-info."""
    info: dict = {"path": exe, "sha": None, "version": None, "ref": None,
                  "upstream_url": None, "pushed": None}
    if not dist:
        return info
    try:
        real = Path(exe).resolve()
        first = real.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return info
    if not first.startswith("#!"):
        return info
    interp = Path(first[2:].strip().split()[0])
    venv = interp.parent.parent
    hits = sorted(venv.glob(f"lib/python*/site-packages/{dist}-*.dist-info"))
    if not hits:
        return info
    di = hits[-1]
    try:
        for line in (di / "METADATA").read_text(encoding="utf-8").splitlines():
            if line.startswith("Version:"):
                info["version"] = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    try:
        du = json.loads((di / "direct_url.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        du = {}
    vcs = du.get("vcs_info") or {}
    info["sha"] = vcs.get("commit_id")
    info["ref"] = vcs.get("requested_revision")
    url = du.get("url")
    if url and url.startswith("file://"):
        local = Path(url[len("file://"):])
        url = run(["git", "remote", "get-url", "origin"], cwd=local) if local.is_dir() else None
    info["upstream_url"] = url
    if (du.get("dir_info") or {}).get("editable") and du.get("url", "").startswith("file://"):
        info.update({k: v for k, v in checkout_info(Path(du["url"][7:])).items()
                     if k != "path" and v is not None})
    return info


def engine_info(name: str, machine: dict, pins: dict, offline: bool,
                cache: dict) -> dict:
    exe_name, dist, pin_key = ENGINES[name]
    engines_cfg = machine.get("engines") if isinstance(machine.get("engines"), dict) else {}
    configured = engines_cfg.get(name)
    if name == "signal-pipeline" and not configured and engines_cfg.get("spotlights-engine"):
        configured = engines_cfg["spotlights-engine"]  # it ships inside spotlights
    pin = pins.get(pin_key) if pin_key else None
    e: dict = {"found": False, "source": None, "path": None, "version": None,
               "sha": None, "ref": None, "pin": pin, "latest_release": None,
               "latest_checked": False, "warnings": []}
    if configured:
        p = Path(str(configured)).expanduser()
        e["source"] = "config"
        if p.is_dir():
            e.update(checkout_info(p))
            e["found"] = True
            if name == "signal-pipeline" and not (p / "src/spotlights_engine/signal_pipeline").is_dir():
                e["warnings"].append(f"{p} has no src/spotlights_engine/signal_pipeline")
        else:
            e["path"] = str(p)
            e["warnings"].append(f"configured path {p} does not exist")
    else:
        exe = shutil.which(exe_name)
        if exe:
            e["source"] = "PATH"
            e.update(installed_info(exe, dist))
            e["found"] = True
    if not e["found"]:
        return e

    pin_ref, pin_sha = split_pin(pin)
    if pin_sha:
        if not e["sha"]:
            e["warnings"].append(f"can't read the installed sha to compare with pin {pin}")
        elif not same_sha(e["sha"], pin_sha):
            e["warnings"].append(
                f"installed {e['sha'][:7]} ({e['ref'] or e['version'] or '?'}) "
                f"differs from the pin {pin}")
    if e.get("pushed") is False:
        e["warnings"].append(
            f"HEAD {(e['sha'] or '')[:7]} on {e['ref'] or 'detached HEAD'} is not on any "
            "remote branch (unpushed); never pin or cite an unpushed engine")
    url = e.get("upstream_url")
    if url and not offline:
        if url not in cache:
            cache[url] = latest_release(url)
        rel = cache[url]
        e["latest_checked"] = rel is not None
        e["latest_release"] = rel
        if rel and rel.get("sha"):
            if not same_sha(e["sha"], rel["sha"]):
                e["warnings"].append(
                    f"installed {(e['sha'] or '?')[:7]} differs from the latest upstream "
                    f"release {rel['tag']} @ {rel['sha'][:7]}")
            if pin_ref and re.fullmatch(r"v?\d+(\.\d+)*", pin_ref) and pin_ref != rel["tag"]:
                e["warnings"].append(f"pin {pin} is not the latest release {rel['tag']}")
    return e


# --- AINS runtime -----------------------------------------------------------

def runtime_constants(init_py: Path) -> dict:
    """Top-level literal constants from the runtime package __init__.py, as text.

    Never imports the module. Raises OSError / SyntaxError / ValueError when
    the file can't be read or parsed.
    """
    tree = ast.parse(init_py.read_text(encoding="utf-8"), filename=str(init_py))
    out: dict = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for t in targets:
            if isinstance(t, ast.Name) and t.id in RUNTIME_CONSTS:
                out[t.id] = ast.literal_eval(value)
    return out


def start_time_method(platform: str | None = None, proc_stat: Path | None = None,
                      which=shutil.which) -> str | None:
    """Mirror of the runtime's procid.method(), without importing it."""
    platform = sys.platform if platform is None else platform
    proc_stat = Path("/proc/self/stat") if proc_stat is None else proc_stat
    if platform.startswith("linux") and proc_stat.is_file():
        return "proc-stat"
    if which("ps") is not None:
        return "ps-lstart"
    return None


def runtime_info(pins: dict, pyyaml: bool, pkg: Path = RUNTIME_PKG,
                 cli: Path = RUNTIME_CLI, method: str | None | bool = False) -> dict:
    init_py = pkg / "__init__.py"
    rt: dict = {"present": init_py.is_file() and cli.is_file(),
                "package": str(pkg), "cli": str(cli), "version": None,
                "store_schema_version": None, "message_schemas": None,
                "export_schema": None, "pin": pins.get("runtime-version"),
                "start_time_method": start_time_method() if method is False else method,
                "available": False, "warnings": []}
    reasons: list[str] = []
    if not rt["present"]:
        missing = [str(p) for p in (init_py, cli) if not p.is_file()]
        reasons.append("runtime files missing: " + ", ".join(missing))
    else:
        try:
            c = runtime_constants(init_py)
        except (OSError, SyntaxError, ValueError) as e:
            c = {}
            reasons.append(f"can't parse {init_py}: {e}")
        v = c.get("RUNTIME_VERSION")
        if isinstance(v, str) and v:
            rt["version"] = v
        elif not reasons:
            reasons.append(f"no RUNTIME_VERSION string in {init_py}")
        if isinstance(c.get("STORE_SCHEMA_VERSION"), int):
            rt["store_schema_version"] = c["STORE_SCHEMA_VERSION"]
        sv = c.get("SCHEMA_VERSIONS")
        if isinstance(sv, dict):
            rt["message_schemas"] = {str(k): list(vs) for k, vs in sv.items()}
        if isinstance(c.get("EXPORT_SCHEMA"), str):
            rt["export_schema"] = c["EXPORT_SCHEMA"]
    if rt["start_time_method"] is None:
        reasons.append("no way to read process start times (no /proc/<pid>/stat "
                       "and no `ps` on PATH); the runtime blocks every launch "
                       "(a pid alone is never trusted)")
    if not pyyaml:
        reasons.append("PyYAML missing (the Nous adapter renders campaign.yaml with it)")
    if rt["pin"] and rt["version"] and rt["pin"] != rt["version"]:
        rt["warnings"].append(f"runtime version {rt['version']} differs from the "
                              f"SKILL.md pin runtime-version {rt['pin']}")
    rt["available"] = not reasons
    if reasons:
        rt["reason"] = "; ".join(reasons)
    return rt


def gate_on_runtime(m: dict, rt: dict) -> dict:
    """Runtime-governed methods are unavailable when the runtime is."""
    if rt["available"]:
        return m
    why = f"runtime unavailable: {rt['reason']}"
    for name in RUNTIME_GOVERNED:
        e = m[name]
        e["reason"] = f"{e['reason']}; {why}" if not e["available"] and e.get("reason") else why
        e["available"] = False
    if m["sim2real-prep"]["available"] and not m["nous-blis"]["available"]:
        m["sim2real-prep"] = {"available": False, "reason": "needs nous-blis (sim2real prep "
                              "follows a Nous win only)"}
    return m


# --- methods ----------------------------------------------------------------

def methods(tools: dict, engines: dict, blis: dict, repo_cfg: dict, cfgs: dict) -> dict:
    m: dict = {}
    m["code-reading"] = {"available": bool(tools["git"]["path"])}
    if not tools["git"]["path"]:
        m["code-reading"]["reason"] = "git not found"

    clusters = repo_cfg.get("clusters") if isinstance(repo_cfg.get("clusters"), dict) else {}
    url_clusters = [k for k, v in clusters.items() if isinstance(v, dict) and v.get("kind") == "url"]
    if not tools["pyyaml"]["available"]:
        m["telemetry-snapshot"] = {"available": False,
                                   "reason": "PyYAML missing (the PromQL query list is YAML)"}
    elif not tools["kubectl"]["path"] and not url_clusters:
        m["telemetry-snapshot"] = {"available": False,
                                   "reason": "no kubectl for port-forward and no `kind: url` "
                                             "cluster in the repo config"}
    else:
        m["telemetry-snapshot"] = {"available": True}
        if not clusters:
            m["telemetry-snapshot"]["note"] = ("no clusters named in the repo config; "
                                               "name one (context/namespace/service or URL) in plan.md")

    sp = engines["spotlights-engine"]
    m["spotlights-static"] = ({"available": True} if sp["found"] else
                              {"available": False, "reason": "spotlights-engine not found "
                               "(not on PATH and no engines.spotlights-engine in the machine config)"})

    sig = engines["signal-pipeline"]
    if not sig["found"]:
        m["spotlights-telemetry"] = {"available": False, "reason": "signal-pipeline not found "
                                     "(not on PATH and no engines.signal-pipeline in the machine config)"}
    else:
        m["spotlights-telemetry"] = {"available": False, "reason": SPOTLIGHTS_TELEMETRY_REASON}

    reasons = []
    if not engines["nous"]["found"]:
        reasons.append("nous not found")
    if not blis["configured"]:
        reasons.append("no BLIS apparatus configured (blis_apparatus_root in the machine config)")
    elif not blis["exists"]:
        reasons.append(f"BLIS apparatus root {blis['apparatus_root']} does not exist")
    if not tools["pyyaml"]["available"]:
        reasons.append("PyYAML missing (collect.py and campaign.yaml need it)")
    if repo_cfg.get("blis_applicable") is False:
        reasons.append("repo config says blis_applicable: false")
    m["nous-blis"] = {"available": not reasons}
    if reasons:
        m["nous-blis"]["reason"] = "; ".join(reasons)

    s2r = engines["sim2real"]
    if not s2r["found"]:
        m["sim2real-prep"] = {"available": False, "reason": "no sim2real checkout configured "
                              "(engines.sim2real in the machine config)"}
    elif not m["nous-blis"]["available"]:
        m["sim2real-prep"] = {"available": False, "reason": "needs nous-blis (sim2real prep "
                              "follows a Nous win only)"}
    else:
        m["sim2real-prep"] = {"available": True}
    return m


# --- main report ------------------------------------------------------------

def tool(name: str, version_args: list[str] | None = None) -> dict:
    p = shutil.which(name)
    out = {"path": p, "version": None}
    if p and version_args:
        v = run([p, *version_args], timeout=10)
        out["version"] = v.splitlines()[0] if v else None
    return out


def free_space(path: Path) -> dict:
    p = path
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        free = shutil.disk_usage(p).free
    except OSError:
        return {"measured_at": str(p), "free_bytes": None, "free_gb": None}
    return {"measured_at": str(p), "free_bytes": free, "free_gb": round(free / 1e9, 1)}


def collect(cwd: str | os.PathLike | None = None, offline: bool = False) -> dict | None:
    """The full report as a dict, or None when cwd is not inside a git repo."""
    top = _config.repo_toplevel(cwd)
    if top is None:
        return None
    try:
        import yaml  # noqa: F401
        pyyaml = {"available": True, "version": getattr(yaml, "__version__", None)}
    except ImportError:
        pyyaml = {"available": False, "version": None}
    tools = {"pyyaml": pyyaml, "git": tool("git", ["--version"]),
             "kubectl": tool("kubectl"), "npx": tool("npx")}
    vi = sys.version_info
    py = {"version": f"{vi[0]}.{vi[1]}.{vi[2]}", "executable": sys.executable,
          "ok": (vi[0], vi[1]) >= (3, 10)}

    cfgs, machine, repo_cfg = config_info(top)
    repo = repo_info(top, repo_cfg)
    pins = read_pins()
    cache: dict = {}
    engines = {n: engine_info(n, machine, pins, offline, cache) for n in ENGINES}

    root = machine.get("blis_apparatus_root")
    blis = {"apparatus_root": str(Path(str(root)).expanduser()) if root else None,
            "configured": bool(root),
            "exists": bool(root) and Path(str(root)).expanduser().is_dir()}
    rroot = _config.runs_root(machine)
    runs = {"root": str(rroot), "study_runs": str(rroot / repo["name"] / "<slug>"),
            "source": ("INVESTIGATION_RUNS" if os.environ.get("INVESTIGATION_RUNS") else
                       "machine config runs_dir" if machine.get("runs_dir") else "default"),
            **free_space(rroot)}

    warnings: list[str] = []
    if not py["ok"]:
        warnings.append(f"Python {py['version']} is older than 3.10")
    if not pyyaml["available"]:
        warnings.append("PyYAML missing: pip install pyyaml (needed by collect.py, "
                        "capture_prom.py and every config file)")
    for c in cfgs.values():
        if c["parseable"] is False:
            warnings.append(f"config {c['path']} can't be parsed: {c['error']}")
    if repo["upstream_remote"] is None:
        warnings.append(f"upstream remote unknown: {repo['upstream_reason']}")
    for n, e in engines.items():
        warnings.extend(f"{n}: {w}" for w in e["warnings"])
    rt = runtime_info(pins, pyyaml["available"])
    warnings.extend(f"runtime: {w}" for w in rt["warnings"])

    unconfigured = []
    if not cfgs["machine"]["present"]:
        unconfigured.append(f"no machine config at {cfgs['machine']['path']} "
                            "(engines found via PATH only; no BLIS apparatus, no sim2real)")
    if not cfgs["repo"]["present"]:
        unconfigured.append("no repo config yet (new_study.py writes it on the first study)")
    if not blis["configured"]:
        unconfigured.append("blis_apparatus_root")
    for n, e in engines.items():
        if not e["found"]:
            unconfigured.append(f"engine {n}")

    return {"doctor_version": DOCTOR_VERSION, "offline": offline, "skill_dir": str(SKILL_DIR),
            "python": py, "tools": tools, "repo": repo, "config": cfgs,
            "pins": pins, "engines": engines, "blis": blis, "runs": runs,
            "runtime": rt,
            "methods": gate_on_runtime(methods(tools, engines, blis, repo_cfg, cfgs), rt),
            "warnings": warnings, "unconfigured": unconfigured}


def human(r: dict) -> str:
    L: list[str] = []
    yes = lambda b: "yes" if b else "no"  # noqa: E731
    L.append("investigation doctor (read-only)")
    L.append("")
    L.append("Tools")
    py = r["python"]
    L.append(f"  python   {py['version']}{'' if py['ok'] else '  (NEEDS 3.10+)'}")
    t = r["tools"]
    L.append(f"  pyyaml   {t['pyyaml']['version'] if t['pyyaml']['available'] else 'MISSING (pip install pyyaml)'}")
    for k in ("git", "kubectl", "npx"):
        v = t[k]
        L.append(f"  {k:<8} {v['path'] or 'not found'}" + (f"  ({v['version']})" if v.get("version") else ""))
    L.append("")
    rp = r["repo"]
    L.append(f"Repo  {rp['name']}  {rp['toplevel']}")
    L.append(f"  branch {rp['branch'] or '(detached)'} @ {(rp['head'] or '?')[:8]}")
    for n, u in rp["remotes"].items():
        tag = " [upstream]" if n == rp["upstream_remote"] else " [fork]"
        L.append(f"  remote {n}: {u}{tag}")
    if not rp["remotes"]:
        L.append("  remotes: none")
    L.append(f"  upstream: {rp['upstream_remote'] or 'UNKNOWN'} ({rp['upstream_reason']})")
    L.append(f"  push remote: {rp['push_remote'] or '?'}"
             + ("  -> UPSTREAM: commit guard will refuse investigations/ commits"
                if rp["push_is_upstream"] else ""))
    L.append("")
    L.append("Config")
    for layer_name, c in r["config"].items():
        state = ("missing" if not c["present"] else
                 "ok" if c["parseable"] else f"UNPARSEABLE: {c['error']}")
        where = c["location"] if c["location"] not in ("machine", "none") else ""
        label = f"{layer_name} ({where})" if where else layer_name
        if c.get("tracking"):
            state += f", tracking: {c['tracking']}"
        L.append(f"  {label:<17} {c['path']}: {state}"
                 + (f"  keys: {', '.join(c['keys'])}" if c["keys"] else ""))
    L.append("")
    L.append("Engines")
    for n, e in r["engines"].items():
        if not e["found"]:
            L.append(f"  {n:<18} not found" + (f" ({e['warnings'][0]})" if e["warnings"] else ""))
            continue
        ver = " ".join(x for x in (e["version"], f"@{e['sha'][:7]}" if e["sha"] else None,
                                   f"({e['ref']})" if e["ref"] else None) if x)
        L.append(f"  {n:<18} {ver or '?'}  via {e['source']}: {e['path']}")
        if e["pin"]:
            L.append(f"  {'':<18} pin {e['pin']}")
        rel = e["latest_release"]
        if rel and rel.get("tag"):
            L.append(f"  {'':<18} latest release {rel['tag']} @ {rel['sha'][:7]}")
        elif e.get("upstream_url") and not r["offline"]:
            L.append(f"  {'':<18} latest release: unknown (ls-remote failed or no tags)")
        for w in e["warnings"]:
            L.append(f"  {'':<18} WARNING: {w}")
    L.append("")
    b = r["blis"]
    L.append(f"BLIS apparatus  {b['apparatus_root'] or 'not configured'}"
             + ("" if not b["configured"] or b["exists"] else "  (MISSING)"))
    ru = r["runs"]
    L.append(f"Runs dir        {ru['root']} ({ru['source']}); free "
             f"{ru['free_gb'] if ru['free_gb'] is not None else '?'} GB")
    L.append("")
    rt = r["runtime"]
    L.append(f"Runtime  {rt['version'] or '?'}"
             + (f" (pin {rt['pin']})" if rt["pin"] else "")
             + ("  available" if rt["available"] else "  UNAVAILABLE"))
    L.append(f"  store schema {rt['store_schema_version'] or '?'}; export "
             f"{rt['export_schema'] or '?'}; start-time method "
             f"{rt['start_time_method'] or 'NONE'}")
    if rt.get("reason"):
        L.append(f"  reason: {rt['reason']}")
    L.append("")
    L.append("Methods")
    for n, m in r["methods"].items():
        L.append(f"  {n:<21} {'available' if m['available'] else 'UNAVAILABLE'}")
        for k in ("reason", "note"):
            if m.get(k):
                L.append(f"  {'':<21} {k}: {m[k]}")
    if r["unconfigured"]:
        L.append("")
        L.append("Unconfigured")
        L.extend(f"  - {u}" for u in r["unconfigured"])
    if r["warnings"]:
        L.append("")
        L.append("Warnings")
        L.extend(f"  - {w}" for w in r["warnings"])
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--offline", action="store_true",
                    help="skip `git ls-remote` latest-release checks")
    a = ap.parse_args(argv)
    r = collect(offline=a.offline)
    if r is None:
        msg = "not inside a git repo: run doctor.py from the repo being investigated"
        if a.json:
            print(json.dumps({"doctor_version": DOCTOR_VERSION, "error": msg}))
        else:
            print(f"doctor: {msg}", file=sys.stderr)
        return 2
    print(json.dumps(r, indent=2) if a.json else human(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
