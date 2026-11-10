#!/usr/bin/env python3
"""Test-only install smoke for the runtime (plan R9; spec §14 "installation
into a separate target repository works without Hive/Crux or checkout-specific
paths"). NOT shipped with the skill. Run by the `install-smoke` CI job:

    python3 tests/_runtime_install_smoke.py <installed-skill-dir> <throwaway-repo>

<installed-skill-dir> is the copy `npx skills add` wrote (for example
<repo>/.claude/skills/investigation). The script:

  1. checks the installed runtime ships only its own adapters (no fake, no
     plugin/env hook to load others) and no checkout path or Hive/Crux name;
  2. commits the install in the throwaway repo (the runtime refuses a dirty
     target), so the target is clean at a known commit;
  3. runs the INSTALLED runtime.py `submit` in a subprocess (no adapter
     involved, so the shipped CLI is exercised as-is);
  4. imports the INSTALLED ains_runtime package (never the checkout's),
     registers the test-only fake adapter from this tests/ dir in-process,
     and calls the installed CLI's main() for `prepare`. The fake builds a
     `fake-engine` argv; nothing is approved or launched.

State goes to temp dirs (INVESTIGATION_RUNS / INVESTIGATION_CONFIG_HOME),
never the user's. Exit 0 = pass; any failure raises.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
SHIPPED_ADAPTERS = {"__init__.py", "base.py", "nous.py", "spotlights_static.py"}
LOADER_HOOKS = ("importlib", "__import__", "entry_points", "runpy", "exec(",
                "AINS_RUNTIME_ADAPTER", "pkgutil")


def fail(msg: str) -> None:
    raise SystemExit(f"install-smoke(runtime): FAIL: {msg}")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.email=smoke@example.com", "-c",
                           "user.name=smoke", "-c", "commit.gpgsign=false", *args],
                          cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


def static_checks(skill: Path) -> None:
    pkg = skill / "scripts" / "ains_runtime"
    if not (skill / "scripts" / "runtime.py").is_file() or not pkg.is_dir():
        fail(f"{skill}: runtime.py or ains_runtime/ not installed")
    adapters = {p.name for p in (pkg / "adapters").glob("*.py")}
    if adapters != SHIPPED_ADAPTERS:
        fail(f"installed adapters are {sorted(adapters)}, expected {sorted(SHIPPED_ADAPTERS)}")
    for p in sorted(pkg.rglob("*.py")) + [skill / "scripts" / "runtime.py"]:
        text = p.read_text(encoding="utf-8")
        for hook in LOADER_HOOKS:
            if hook in text:
                fail(f"{p}: contains {hook!r} (the runtime must not load adapters "
                     "from outside the shipped package)")
        if "FakeAdapter" in text:
            fail(f"{p}: references the test-only fake adapter")
    for p in sorted(skill.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            text = p.read_text(encoding="utf-8", errors="replace")
            if str(CHECKOUT) in text:
                fail(f"{p}: contains the checkout path {CHECKOUT}")
            low = text.lower()
            for name in ("import hive", "import crux", "from hive", "from crux"):
                if name in low:
                    fail(f"{p}: depends on Hive/Crux ({name!r})")


def request(repo_name: str, commit: str) -> dict:
    return {
        "schema": "ains.experiment-request/v1", "message_id": "req-smoke-1",
        "work_id": "w-smoke-1", "revision": 1,
        "target": {"repo_name": repo_name, "commit": commit}, "artifacts": [],
        "body": {"capability": "nous", "question": "Install smoke: does prepare work?",
                 "task_type": "other", "objective": "prove prepare runs from an install",
                 "locks": {}, "limits": {"max_iterations": 1},
                 "semantics": {"models": {"session": "smoke-model"},
                               "run_id": "install-smoke-run-0001"}},
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    skill, repo = Path(argv[0]).resolve(), Path(argv[1]).resolve()
    static_checks(skill)

    state = Path(tempfile.mkdtemp(prefix="ains-install-smoke-"))
    os.environ["INVESTIGATION_RUNS"] = str(state / "runs")
    os.environ["INVESTIGATION_CONFIG_HOME"] = str(state / "config")
    os.environ.pop("XDG_CONFIG_HOME", None)

    study = repo / "investigations" / "smoke"
    study.mkdir(parents=True, exist_ok=True)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "install the investigation skill (smoke)")
    commit = git(repo, "rev-parse", "HEAD")
    msg = study / "request.json"     # under investigations/: ignored by the clean check
    msg.write_text(json.dumps(request(repo.name, commit), indent=2))

    # 3. The shipped CLI, as installed, in a subprocess.
    rt = skill / "scripts" / "runtime.py"
    r = subprocess.run([sys.executable, str(rt), "--study", str(study), "submit", str(msg)],
                       capture_output=True, text=True, cwd=repo)
    if r.returncode != 0 or json.loads(r.stdout)["body"].get("status") != "accepted":
        fail(f"installed runtime.py submit: exit {r.returncode}\n{r.stdout}{r.stderr}")
    r2 = subprocess.run([sys.executable, str(rt), "--study", str(study), "submit", str(msg)],
                        capture_output=True, text=True, cwd=repo)
    if r2.returncode != 0 or r2.stdout != r.stdout:
        fail("a duplicate submit did not return the same receipt")

    # 4. The installed package, with the fake adapter registered in-process.
    # Like the shipped entry scripts: no __pycache__ inside the target (D4).
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(skill / "scripts"))
    import ains_runtime  # noqa: E402
    if Path(ains_runtime.__file__).resolve().parent != (skill / "scripts" / "ains_runtime"):
        fail(f"imported {ains_runtime.__file__}, not the installed copy")
    from ains_runtime import adapters as A  # noqa: E402
    if A.names() != ["nous", "spotlights-static"]:
        fail(f"installed registry is {A.names()}")
    sys.path.insert(1, str(TESTS))
    import _runtime_fake_adapter as fake  # noqa: E402
    if Path(fake.A.__file__).resolve().parent != skill / "scripts" / "ains_runtime" / "adapters":
        fail("the fake adapter bound to a checkout copy of ains_runtime")
    _, restore = fake.use()
    from ains_runtime.cli import main as cli_main  # noqa: E402
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli_main(["--study", str(study), "prepare", "w-smoke-1", "--json"])
    finally:
        restore()
    if rc != 0:
        fail(f"prepare exit {rc}: {err.getvalue()}")
    res = json.loads(out.getvalue())
    card, card_path = res["card"], Path(res["card_path"])
    if card_path.read_text(encoding="utf-8") != card:
        fail("card.md differs from the printed card")
    if res["short_code"] not in card or "fake-engine" not in card:
        fail("card lacks the short code or the prepared argv")
    for text, what in ((card, "card"), (out.getvalue(), "prepare output")):
        if str(CHECKOUT) in text:
            fail(f"{what} contains the checkout path {CHECKOUT}")
    if state not in card_path.parents:
        fail(f"preparation written outside the temp runs dir: {card_path}")
    dirty = git(repo, "status", "--porcelain", "--untracked-files=normal")
    dirty = [ln for ln in dirty.splitlines() if not ln[3:].startswith("investigations/")]
    if dirty:
        fail(f"submit/prepare left the target dirty: {dirty[:5]}")
    leaked = sorted(m for m in sys.modules if m.split(".")[0].lower() in ("hive", "crux"))
    if leaked:
        fail(f"Hive/Crux modules imported: {leaked}")
    print(f"install-smoke(runtime): PASS submit+prepare from {skill} "
          f"(prep {res['prep_id']}, short code {res['short_code']}); nothing approved "
          "or launched")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
