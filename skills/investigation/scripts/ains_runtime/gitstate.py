"""Target repository revision and clean-tree checks (plan §1 Q6, decision D4).

Policy, enforced at prepare and again before launch:

  1. The target is clean: `git status --porcelain --untracked-files=normal`
     over the whole tree, excluding `investigations/` (the study is written
     there, and committed in fork mode) and `.nous-experiments/` (Nous
     worktrees). Any other change -- modified, staged, untracked -- blocks.
  2. HEAD at launch equals the prepared commit.
  3. The material record stores the commit and the clean-tree result.

A dirty tree is rejected, never "approximated by HEAD" (spec §15).

Git is run through _config.git (no shell, 30 s timeout, None on failure).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import _config  # noqa: E402
import upstreams as U  # noqa: E402  (references/upstreams/: the only home of upstream facts)

# D4: the only paths a dirty target may have changes under. The Nous
# worktree dir is the nous.json worktree_dir fact — the same value
# adapters/nous.py sources — so a Nous bump renaming it changes the
# exclusion with the adapter, not apart from it.
IGNORED_PATHS = ("investigations", U.fact("nous", "worktree_dir"))
DIRTY_SAMPLE = 20


class GitStateError(Exception):
    """The target is not a git repo, or git can't be run."""


@dataclass
class GitState:
    toplevel: Path
    commit: str
    dirty: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.dirty

    def material(self) -> dict:
        """The part of the git state that goes into the material record."""
        return {"commit": self.commit, "clean": self.clean,
                "clean_policy": {"untracked": "normal",
                                 "ignored_paths": list(IGNORED_PATHS)}}


def toplevel(path: str | Path) -> Path:
    top = _config.repo_toplevel(path)
    if top is None:
        raise GitStateError(f"{path}: not inside a git work tree")
    return top


def head_commit(repo: str | Path) -> str:
    out = _config.git("rev-parse", "--verify", "HEAD^{commit}", cwd=repo)
    if not out:
        raise GitStateError(f"{repo}: no HEAD commit (empty repo or git failed)")
    return out


def dirty_paths(repo: str | Path) -> list[str]:
    """Porcelain lines for changes outside the D4-ignored paths ([] = clean)."""
    excludes = [f":(exclude,top){p}" for p in IGNORED_PATHS]
    out = _config.git("status", "--porcelain", "--untracked-files=normal",
                      "--ignore-submodules=none", "--", ":(top)", *excludes, cwd=repo)
    if out is None:
        raise GitStateError(f"{repo}: git status failed")
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def snapshot(repo: str | Path) -> GitState:
    top = toplevel(repo)
    return GitState(toplevel=top, commit=head_commit(top), dirty=dirty_paths(top))


def problems(state: GitState, *, expected_commit: str | None = None) -> list[str]:
    """Reasons this state can't be prepared/launched ([] = ok)."""
    out = []
    if expected_commit is not None and state.commit != expected_commit:
        out.append(f"target HEAD is {state.commit}, expected {expected_commit}")
    if state.dirty:
        sample = ", ".join(state.dirty[:DIRTY_SAMPLE])
        more = (f" (+{len(state.dirty) - DIRTY_SAMPLE} more)"
                if len(state.dirty) > DIRTY_SAMPLE else "")
        out.append(f"target has uncommitted changes outside "
                   f"{'/, '.join(IGNORED_PATHS)}/: {sample}{more}")
    return out
