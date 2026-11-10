#!/usr/bin/env python3
"""Scaffold investigations/<slug>/ in the session repo, and guard commits to it.

    new_study.py --slug S [--question TEXT] [--tracking fork|untracked]
    new_study.py --commit-guard

Scaffold: creates <toplevel>/investigations/<slug>/{question.md, plan.md,
hand-score.md} from the skill's assets/, with question.md pinned to the repo's
remote URL, branch, HEAD sha and the engine versions doctor.py reports.

On the first study in a repo (no per-repo config yet) the tracking mode is
needed: --tracking, or an interactive prompt when stdin is a TTY; otherwise it
is an error.
  fork       writes investigations/config.yaml (committed, on your fork only)
  untracked  writes <config home>/repos/<repo>.yaml and adds `investigations/`
             to .git/info/exclude (local, never committed)

--commit-guard exits 0 when committing investigations/ on the current branch
is allowed, 1 when it isn't (untracked mode, or the push remote is the
upstream remote, or upstream can't be determined), 2 on usage errors.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True  # an install inside a target repo must not dirty it (D4)
sys.path.insert(0, str(HERE))
import _config  # noqa: E402
import doctor  # noqa: E402

ASSETS = HERE.parent / "assets"
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")
EXCLUDE_LINE = "investigations/"
TRACKING_MODES = ("fork", "untracked")


class StudyError(Exception):
    pass


def render(template: str, values: dict) -> str:
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(values.get(m.group(1), m.group(0))), template)


def read_asset(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


def exclude_path(top: Path) -> Path:
    p = _config.git("rev-parse", "--git-path", "info/exclude", cwd=top)
    path = Path(p) if p else Path(".git/info/exclude")
    return path if path.is_absolute() else top / path


def add_exclude(top: Path) -> bool:
    """Add `investigations/` to .git/info/exclude. Idempotent; True if it was added."""
    path = exclude_path(top)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    if any(line.strip() in (EXCLUDE_LINE, "/" + EXCLUDE_LINE) for line in text.splitlines()):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text + EXCLUDE_LINE + "\n", encoding="utf-8")
    return True


def existing_repo_config(top: Path) -> tuple[Path | None, dict]:
    for p in _config.repo_config_paths(top):
        if p.is_file():
            return p, _config._load_mapping(p)
    return None, {}


def ask_tracking(repo: dict) -> str:
    print("First study in this repo: choose a tracking mode for investigations/.", file=sys.stderr)
    print(f"  remotes: {', '.join(f'{k}={v}' for k, v in repo['remotes'].items()) or 'none'}",
          file=sys.stderr)
    print(f"  upstream: {repo['upstream_remote'] or 'unknown'}; push remote: "
          f"{repo['push_remote'] or 'unknown'}", file=sys.stderr)
    print("  fork      commit investigations/ on your fork only (default)", file=sys.stderr)
    print("  untracked keep studies on disk only (.git/info/exclude)", file=sys.stderr)
    while True:
        print("tracking [fork/untracked] (fork): ", end="", file=sys.stderr, flush=True)
        line = sys.stdin.readline()
        if not line:
            raise StudyError("no tracking mode given")
        ans = line.strip().lower() or "fork"
        if ans in TRACKING_MODES:
            return ans


def ensure_repo_config(top: Path, repo: dict, tracking_flag: str | None) -> tuple[str, list[str]]:
    """Return (tracking mode, notes). Writes the repo config on the first study."""
    notes: list[str] = []
    path, cfg = existing_repo_config(top)
    if path is not None:
        mode = cfg.get("tracking")
        if mode not in TRACKING_MODES:
            raise StudyError(f"{path}: `tracking` must be fork or untracked, got {mode!r}")
        if tracking_flag and tracking_flag != mode:
            raise StudyError(f"{path} already says tracking: {mode}; edit it by hand to change "
                             f"modes (asked for {tracking_flag})")
        if mode == "untracked" and add_exclude(top):
            notes.append(f"added {EXCLUDE_LINE} to {exclude_path(top)}")
        return mode, notes

    mode = tracking_flag
    if mode is None:
        if sys.stdin.isatty():
            mode = ask_tracking(repo)
        else:
            raise StudyError("first study in this repo: pass --tracking fork|untracked "
                             "(ask the owner which; stdin is not a TTY, so I can't prompt)")
    upstream = repo.get("upstream_remote") or "upstream"
    text = render(read_asset("config.yaml.tmpl"),
                  {"repo_name": repo["name"], "tracking": mode, "upstream_remote": upstream})
    if mode == "fork":
        dest = top / "investigations" / "config.yaml"
    else:
        dest = _config.config_home() / "repos" / f"{repo['name']}.yaml"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    notes.append(f"wrote {dest} (tracking: {mode}, upstream_remote: {upstream})")
    if repo.get("upstream_remote") is None:
        notes.append("upstream remote could not be determined; set upstream_remote in "
                     f"{dest} before committing")
    if mode == "untracked" and add_exclude(top):
        notes.append(f"added {EXCLUDE_LINE} to {exclude_path(top)}")
    return mode, notes


def engine_rows(report: dict) -> str:
    rows = []
    for name, e in report["engines"].items():
        if e["found"]:
            parts = [e.get("version"), f"@ {e['sha'][:12]}" if e.get("sha") else None,
                     f"({e['ref']})" if e.get("ref") else None, f"via {e['source']}"]
            val = " ".join(p for p in parts if p)
            if e.get("pin"):
                val += f"; pin {e['pin']}"
            if e.get("warnings"):
                val += "; WARN: " + " | ".join(e["warnings"])
        else:
            val = "not found"
        rows.append(f"| Engine `{name}` | {val} |")
    return "\n".join(rows)


def method_lists(report: dict) -> tuple[str, str]:
    av = [n for n, m in report["methods"].items() if m["available"]]
    un = [f"{n} ({m.get('reason', '')})" for n, m in report["methods"].items() if not m["available"]]
    return ", ".join(av) or "none", "; ".join(un) or "none"


def scaffold(slug: str, question: str | None, tracking: str | None, cwd=None) -> dict:
    if not SLUG_RE.fullmatch(slug):
        raise StudyError(f"bad slug {slug!r}: lowercase letters, digits, '.', '_' and '-' only")
    report = doctor.collect(cwd=cwd, offline=True)
    if report is None:
        raise StudyError("not inside a git repo")
    top = Path(report["repo"]["toplevel"])
    repo = report["repo"]
    study = top / "investigations" / slug
    if study.exists():
        raise StudyError(f"{study} already exists; pick another slug")
    mode, notes = ensure_repo_config(top, repo, tracking)

    remote_url = repo["remotes"].get(repo.get("upstream_remote") or "") \
        or repo["remotes"].get("origin") or next(iter(repo["remotes"].values()), "none")
    available, unavailable = method_lists(report)
    values = {
        "slug": slug,
        "question": question or "TODO: the question, in your words",
        "date": dt.date.today().isoformat(),
        "repo_name": repo["name"],
        "remote_url": remote_url,
        "branch": repo.get("branch") or "(detached)",
        "head": repo.get("head") or "unknown",
        "tracking": mode,
        "runs_dir": str(_config.runs_dir(repo["name"], slug)),
        "engine_rows": engine_rows(report),
        "available_methods": available,
        "unavailable_methods": unavailable,
    }
    study.mkdir(parents=True)
    for tmpl, out in (("question.md.tmpl", "question.md"), ("plan.md.tmpl", "plan.md"),
                      ("hand-score.md.tmpl", "hand-score.md")):
        (study / out).write_text(render(read_asset(tmpl), values), encoding="utf-8")
    if mode == "fork" and repo.get("push_is_upstream"):
        notes.append(f"push remote {repo['push_remote']!r} is the upstream remote: the commit "
                     "guard will refuse to commit investigations/ until you push to a fork")
    return {"study": str(study), "tracking": mode, "notes": notes}


def commit_guard(cwd=None) -> tuple[bool, str]:
    report = doctor.collect(cwd=cwd, offline=True)
    if report is None:
        return False, "not inside a git repo"
    repo = report["repo"]
    top = Path(repo["toplevel"])
    path, cfg = existing_repo_config(top)
    mode = cfg.get("tracking") if path else None
    if mode == "untracked":
        return False, "tracking is untracked: investigations/ stays on disk and is never committed"
    if not repo.get("branch"):
        return False, "detached HEAD: no branch, so no push remote to check"
    if repo.get("upstream_remote") is None:
        return False, f"upstream remote unknown ({repo['upstream_reason']}); set upstream_remote " \
                      "in the repo config"
    if not repo.get("push_remote"):
        return False, f"branch {repo['branch']} has no push remote"
    if repo["push_is_upstream"]:
        return False, (f"branch {repo['branch']} pushes to {repo['push_remote']!r}, the upstream "
                       "remote: never commit investigations/ there (push to your fork: "
                       f"git config branch.{repo['branch']}.pushRemote <fork>)")
    note = "" if mode else " (no repo config yet; assuming fork)"
    return True, (f"ok: branch {repo['branch']} pushes to {repo['push_remote']!r}, not upstream "
                  f"{repo['upstream_remote']!r}{note}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--slug")
    ap.add_argument("--question")
    ap.add_argument("--tracking", choices=TRACKING_MODES)
    ap.add_argument("--commit-guard", action="store_true",
                    help="exit 0 if committing investigations/ is allowed here, else 1")
    a = ap.parse_args(argv)
    if a.commit_guard:
        ok, msg = commit_guard()
        print(f"commit-guard: {msg}", file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if not a.slug:
        ap.error("--slug is required (or use --commit-guard)")
    try:
        res = scaffold(a.slug, a.question, a.tracking)
    except (StudyError, _config.ConfigError) as e:
        print(f"new_study: {e}", file=sys.stderr)
        return 2
    print(f"created {res['study']} (tracking: {res['tracking']})")
    for n in res["notes"]:
        print(f"  - {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
