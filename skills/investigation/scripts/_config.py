"""Shared config loader for the investigation skill's scripts.

Two layers (see SKILL.md / references):

  machine  ~/.config/investigation/config.yaml        never committed
           keys: runs_dir, blis_apparatus_root,
                 engines: {nous, spotlights-engine, signal-pipeline, sim2real}  (checkout paths)
                 repos:   {<repo-name>: <path>}         (for --check-citations)
                 models:  {nous_session, spotlights_claude, spotlights_codex}
                          (defaults the agent puts in runtime requests)
  repo     <toplevel>/investigations/config.yaml      committed (tracking: fork)
           or ~/.config/investigation/repos/<repo>.yaml (tracking: untracked)
           keys: tracking (fork|untracked), upstream_remote, clusters, promql_extra,
                 blis_applicable, target_metrics

The config home is $INVESTIGATION_CONFIG_HOME, else $XDG_CONFIG_HOME/investigation,
else ~/.config/investigation. PyYAML is imported lazily, only when a YAML file is
actually parsed, so stdlib-only callers (doctor.py, validate_answer.py) can import
this module and report a missing PyYAML instead of crashing on it.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

DEFAULT_RUNS = "~/.cache/investigation"


class ConfigError(Exception):
    """A config file exists but can't be read or parsed, or PyYAML is missing."""


def yaml_available() -> bool:
    try:
        import yaml  # noqa: F401
    except ImportError:
        return False
    return True


def load_yaml(path: Path) -> Any:
    """Parse a YAML file. Raises ConfigError on a missing PyYAML or bad YAML."""
    try:
        import yaml
    except ImportError as e:
        raise ConfigError("PyYAML is not installed (pip install pyyaml)") from e
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"{path}: {e}") from e


def config_home() -> Path:
    if os.environ.get("INVESTIGATION_CONFIG_HOME"):
        return Path(os.environ["INVESTIGATION_CONFIG_HOME"]).expanduser()
    if os.environ.get("XDG_CONFIG_HOME"):
        return Path(os.environ["XDG_CONFIG_HOME"]).expanduser() / "investigation"
    return Path.home() / ".config" / "investigation"


def machine_config_path() -> Path:
    return config_home() / "config.yaml"


def _load_mapping(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = load_yaml(path)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def load_machine_config() -> dict:
    return _load_mapping(machine_config_path())


# --- git / repo -------------------------------------------------------------

def git(*args: str, cwd: str | os.PathLike | None = None) -> str | None:
    """Run git; return stripped stdout, or None on failure / git missing."""
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                           text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def repo_toplevel(cwd: str | os.PathLike | None = None) -> Path | None:
    out = git("rev-parse", "--show-toplevel", cwd=cwd)
    return Path(out) if out else None


def repo_name(toplevel: Path) -> str:
    """Basename of the origin remote URL (minus .git), else the folder name."""
    url = git("remote", "get-url", "origin", cwd=toplevel)
    if url:
        base = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        if base.endswith(".git"):
            base = base[:-4]
        if base:
            return base
    return toplevel.name


def repo_config_paths(toplevel: Path) -> list[Path]:
    """Candidate per-repo config files, in precedence order."""
    return [toplevel / "investigations" / "config.yaml",
            config_home() / "repos" / f"{repo_name(toplevel)}.yaml"]


def load_repo_config(toplevel: Path) -> dict:
    """The first per-repo config that exists (committed file wins)."""
    for p in repo_config_paths(toplevel):
        if p.is_file():
            return _load_mapping(p)
    return {}


# --- runs dir ---------------------------------------------------------------

def runs_root(machine: dict | None = None) -> Path:
    """$INVESTIGATION_RUNS, else machine config runs_dir, else ~/.cache/investigation."""
    if os.environ.get("INVESTIGATION_RUNS"):
        return Path(os.environ["INVESTIGATION_RUNS"]).expanduser()
    if machine is None:
        try:
            machine = load_machine_config()
        except ConfigError:
            machine = {}
    return Path(str(machine.get("runs_dir") or DEFAULT_RUNS)).expanduser()


def runs_dir(repo: str, slug: str, machine: dict | None = None) -> Path:
    """$INVESTIGATION_RUNS/<repo>/<slug>/ — outside the repo, never committed."""
    return runs_root(machine) / repo / slug
