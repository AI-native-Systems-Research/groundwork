"""ains_runtime's sys.path guards make the package importable from outside.

Every ains_runtime module that imports a scripts/ sibling (`upstreams`,
`_config`, ...) at module level first inserts scripts/ on sys.path:

    _SCRIPTS = Path(__file__).resolve().parent.parent
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))

The tests all import ains_runtime with scripts/ already on sys.path (via
tests/_runtime_util.py), so the insert arm never runs in CI's coverage run
(cli.py:36, gitstate.py:24, nous.py:60, spotlights_static.py:90, results.py:77
at main 8d40c69) — and the guards are load-bearing, not dead: a process that
resolves ains_runtime without scripts/ on the path (a site-packages-style
import: symlink, editable install, or a PYTHONPATH entry inside the package
tree) gets `ModuleNotFoundError: No module named 'upstreams'` / `'_config'`
the moment a guard is removed (verified by mutation at main 8d40c69: dropping
nous's guard breaks nous, spotlights_static and results; dropping gitstate's
or cli's breaks each; spotlights_static's and results's own inserts are
shadowed by import order — adapters/__init__ loads nous first — but they are
the same class and pin the same contract).

These tests import the package the way such a process does — through a
symlinked site dir on PYTHONPATH, cwd outside the repo — so the true arm
runs, and the sibling imports resolve through the package's own location
wherever the process found it. No production code is changed.
"""
import ast
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runtime_util import SCRIPTS  # noqa: E402

PKG = SCRIPTS / "ains_runtime"

# Modules whose module-level body imports a scripts/ sibling through the
# guard: (module, the sibling import that must resolve).
GUARDED_IMPORTS = [
    ("ains_runtime.adapters.nous", "upstreams"),
    ("ains_runtime.adapters.spotlights_static", "upstreams"),
    ("ains_runtime.gitstate", "_config"),
    ("ains_runtime.cli", "_config"),
]
# Also importable from outside (results.py's guard serves its deferred
# sibling imports; the rest import cleanly through the package alone).
FOREIGN_IMPORTABLE = [m for m, _ in GUARDED_IMPORTS] + [
    "ains_runtime.results", "ains_runtime.preparation", "ains_runtime.supervisor",
    "ains_runtime.recovery", "ains_runtime.store", "ains_runtime.contracts",
    "ains_runtime.adapters", "ains_runtime.adapters.base",
]


def run_child(code: str, cwd: Path) -> subprocess.CompletedProcess:
    """Run python -c CODE with a site dir holding only `ains_runtime`.

    The symlinked site dir reproduces how any process outside this checkout
    can resolve the package without scripts/ ever being on sys.path: a
    site-packages install, an editable install, or a PYTHONPATH entry inside
    the package tree. cwd is outside the repo so neither the site dir nor an
    implicit '' can stand in for the guard. COVERAGE_* keys are forwarded so
    the child is measured like every other subprocess test.
    """
    site = Path(tempfile.mkdtemp(prefix="ains-guard-site-"))
    try:
        os.symlink(PKG, site / "ains_runtime")
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["PYTHONPATH"] = str(site)
        return subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, env=env, cwd=str(cwd), timeout=120)
    finally:
        shutil.rmtree(site, ignore_errors=True)


class ForeignImport(unittest.TestCase):
    def setUp(self):
        self.cwd = Path(tempfile.mkdtemp(prefix="ains-guard-cwd-"))
        self.addCleanup(shutil.rmtree, self.cwd, ignore_errors=True)

    def test_every_module_imports_from_outside_the_checkout(self):
        # Each import runs in its own child: the first guard to run inserts
        # scripts/, so later modules in one process would see it present and
        # the arm would not run for them (import order shadows — that is why
        # spotlights_static.py:90 and results.py:77 stay unreached at main).
        for module in FOREIGN_IMPORTABLE:
            with self.subTest(module=module):
                sibling = dict(GUARDED_IMPORTS).get(module)
                code = f"import {module}, sys\n"
                if sibling:
                    code += f"print({sibling!r} in sys.modules)\n"
                r = run_child(code, self.cwd)
                self.assertEqual(r.returncode, 0, r.stderr)
                if sibling:
                    # The sibling import at module level resolved (and was
                    # used), proving the package's guard put scripts/ on the
                    # path: nothing else could have.
                    self.assertIn("True", r.stdout, r.stderr)

    def test_sibling_resolves_through_the_packages_own_location(self):
        # The guard must resolve the sibling to THIS checkout's scripts/
        # (Path(__file__) of the imported package), not to whatever else a
        # foreign sys.path might carry. Import through a decoy site dir that
        # also contains a fake `upstreams.py`, proving the real one wins.
        site = Path(tempfile.mkdtemp(prefix="ains-guard-decoy-"))
        try:
            os.symlink(PKG, site / "ains_runtime")
            (site / "upstreams.py").write_text("raise SystemExit('decoy upstreams')\n")
            env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            env["PYTHONPATH"] = str(site)
            code = ("import ains_runtime.adapters.nous as N, upstreams as U\n"
                    "print(U.__file__)\n")
            r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                               text=True, env=env, cwd=str(self.cwd), timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            # The guard inserts scripts/ at position 0, ahead of the decoy
            # dir — the resolved upstreams is the checkout's.
            self.assertEqual(Path(r.stdout.strip()).resolve(),
                             (SCRIPTS / "upstreams.py").resolve())
        finally:
            shutil.rmtree(site, ignore_errors=True)

    def test_runtime_entry_runs_from_a_foreign_cwd_without_pythonpath(self):
        # The shipped entry script (runtime.py) is how the installed skill
        # runs; from a cwd outside the repo with no PYTHONPATH it must still
        # reach --version. This pins the entry's own insert plus the guards
        # it relies on downstream.
        env = {k: v for k, v in os.environ.items()
               if k != "PYTHONPATH" and not k.startswith("PYTHON")}
        r = subprocess.run([sys.executable, str(SCRIPTS / "runtime.py"), "--version"],
                           capture_output=True, text=True, env=env, cwd=str(self.cwd),
                           timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ains-runtime", r.stdout)


class GuardInventory(unittest.TestCase):
    """Static pin: the guarded set stays complete.

    A new module in ains_runtime that imports a scripts/ sibling at module
    level must carry the same guard (or the foreign import above starts
    failing for it); a removed module must leave the list. This catches the
    class at review time instead of as a site-packages-shaped
    ModuleNotFoundError. Deferred imports (inside functions) are excluded:
    they run after some guarded module has already put scripts/ on the path
    — preparation.py's `import _config` says so in a comment.
    """

    def test_every_module_level_sibling_import_carries_the_guard(self):
        siblings = {p.stem for p in SCRIPTS.glob("*.py")}
        guarded = []
        for p in sorted(PKG.rglob("*.py")):
            tree = ast.parse(p.read_text(encoding="utf-8"))
            imported = set()
            for n in tree.body:                      # module level only
                if isinstance(n, ast.Import):
                    imported |= {a.name.split(".")[0] for a in n.names}
                elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                    imported.add(n.module.split(".")[0])
            if imported & siblings:
                self.assertIn("sys.path.insert", p.read_text(encoding="utf-8"),
                              f"{p.relative_to(SCRIPTS)} imports a scripts/ sibling "
                              "at module level without the sys.path guard")
                guarded.append(p.relative_to(SCRIPTS).as_posix())
        self.assertEqual(sorted(guarded), sorted(
            m.replace(".", "/") + ".py" for m, _ in GUARDED_IMPORTS))

    def test_the_guard_inserts_the_siblings_directory(self):
        # The guard must compute scripts/ from __file__ (the package's own
        # location), so an installed copy resolves its own siblings. A guard
        # that hardcoded a path or used cwd would break the foreign import.
        for rel in ("ains_runtime/cli.py", "ains_runtime/gitstate.py",
                    "ains_runtime/adapters/nous.py",
                    "ains_runtime/adapters/spotlights_static.py",
                    "ains_runtime/results.py"):
            text = (SCRIPTS / rel).read_text(encoding="utf-8")
            with self.subTest(module=rel):
                self.assertIn("Path(__file__).resolve().parent", text)
                self.assertIn("if str(", text)


if __name__ == "__main__":
    unittest.main()
