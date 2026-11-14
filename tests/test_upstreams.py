"""references/upstreams/ is the only home of upstream versions and facts.

1. Every facts file was verified against the current lock entry.
2. Generated docs (method-plan.md's representability table) match blis.json.
3. No code outside upstreams/ restates an upstream version or cites upstream
   source by line number.
4. The Nous adapter accepts only BLIS builds and catalog commits in the lock.
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "investigation"
sys.path.insert(0, str(SKILL / "scripts"))

import upstreams as U  # noqa: E402


class LockAndFacts(unittest.TestCase):
    def test_every_facts_file_matches_its_lock_entry(self):
        for name, ent in U.lock().items():
            with self.subTest(upstream=name):
                data = json.loads((U.UPSTREAMS_DIR / ent["facts"]).read_text())
                self.assertEqual(
                    data.get("verified_against"), U.lock_id(name),
                    f"{ent['facts']} is stale: re-verify it against the new lock entry, "
                    f"then set verified_against to {U.lock_id(name)!r}")

    def test_every_fact_has_a_value_and_a_source(self):
        for name in U.lock():
            for key, f in U.facts(name)["facts"].items():
                with self.subTest(upstream=name, fact=key):
                    self.assertIn("value", f)
                    self.assertTrue(str(f.get("source") or "").strip())

    def test_editing_a_lock_entry_changes_its_id(self):
        ent = dict(U.entry("nous"), commit="0" * 40)
        self.assertNotEqual(U.lock_id("nous", ent), U.lock_id("nous"))

    def test_stale_facts_are_refused(self):
        real = U.lock_id
        U.facts.cache_clear()
        try:
            U.lock_id = lambda name, ent=None: f"{name}@000000000000"
            with self.assertRaisesRegex(U.UpstreamError, "re-verify"):
                U.facts("nous")
        finally:
            U.lock_id = real
            U.facts.cache_clear()

    def test_binaries_are_fully_described(self):
        for bid, b in U.entry("blis")["binaries"].items():
            with self.subTest(binary=bid):
                self.assertRegex(b["sha256"], r"\A[0-9a-f]{64}\Z")
                self.assertRegex(b["source_commit"], r"\A[0-9a-f]{40}\Z")
                self.assertTrue(b["file"] and b["router"])
                self.assertIsInstance(b["patches"], list)
        self.assertRegex(U.entry("blis")["catalog"]["commit"], r"\A[0-9a-f]{40}\Z")

    def test_engine_commits_are_full(self):
        for name in ("nous", "spotlights", "sim2real", "vllm", "llm-d-router"):
            self.assertRegex(U.entry(name)["commit"], r"\A[0-9a-f]{40}\Z")


class GeneratedDocs(unittest.TestCase):
    def test_method_plan_table_is_generated_from_blis_json(self):
        text = (SKILL / "references" / "method-plan.md").read_text()
        self.assertIn(U.representability_table_md(), text,
                      "method-plan.md's representability table differs from blis.json; "
                      "regenerate it: python3 scripts/verify_upstreams.py --write-docs")


class NoRestatedUpstreamFacts(unittest.TestCase):
    """Code may not restate a locked version or cite upstream source lines:
    such facts belong in references/upstreams/<name>.json."""

    SCAN = [SKILL / "SKILL.md", *sorted((SKILL / "scripts").rglob("*.py"))]
    LINE_CITE = re.compile(r"\b[A-Za-z_][\w/]*\.(py|go):\d+")

    def test_no_locked_commit_outside_upstreams(self):
        shas = {U.entry(n)["commit"][:7] for n in ("nous", "spotlights")}
        shas |= {b["sha256"][:12] for b in U.entry("blis")["binaries"].values()}
        shas.add(U.entry("blis")["catalog"]["commit"][:12])
        for path in self.SCAN:
            text = path.read_text()
            for s in shas:
                with self.subTest(file=str(path.relative_to(ROOT)), sha=s):
                    self.assertNotIn(s, text)

    def test_banner_shas_are_locked(self):
        """Every version banner in the shipped reference docs and assets cites
        a commit lock.json holds, so a lock bump can flag banner drift (the
        gap guide's beads 1a88cf82-187 / e4fa169d-27e recorded four banners no
        lock entry covered). The one exception is spotlights.md's `e7aff25`:
        an unpushed `skill/spotlights-run` branch commit that the design's
        pin policy (docs/design-v2.md) says is never pinned — the banner must
        keep saying so inline, and the operative facts record is
        spotlights.json's verified_against (the v0.1.1 pin), not the banner."""
        banners = {
            "references/sim2real.md": ["28a8e98"],
            "references/telemetry.md": ["2f1cba799e", "618eb0a9"],
            "assets/promql/starter.yaml": ["2f1cba799e", "618eb0a9"],
        }
        for rel, shas in banners.items():
            text = (SKILL / rel).read_text()
            for sha in shas:
                with self.subTest(file=rel, sha=sha):
                    self.assertIn(sha, text)
                    owner = next((n for n in U.lock()
                                  if str(U.entry(n).get("commit", "")).startswith(sha)), None)
                    self.assertIsNotNone(
                        owner, f"{rel}: banner sha {sha} is not in lock.json; "
                        "add an entry (with a facts file) or fix the banner")
        # The unpinnable spotlights banner keeps its self-qualifying text.
        text = (SKILL / "references" / "spotlights.md").read_text()
        self.assertIn("e7aff25", text)
        self.assertIn("not the `v0.1.1`", text)
        self.assertNotIn("e7aff25", json.dumps(U.lock()),
                         "an unpushed branch commit must never be pinned")

    def test_no_upstream_line_citations_in_code(self):
        for path in self.SCAN:
            hits = self.LINE_CITE.findall(path.read_text())
            with self.subTest(file=str(path.relative_to(ROOT))):
                self.assertEqual(hits, [], "cite upstream source in the facts file instead")


class LockEnforced(unittest.TestCase):
    def setUp(self):
        from ains_runtime.adapters import nous as N
        self.N = N
        self.binary = U.entry("blis")["binaries"]["stock"]
        self.catalog = U.entry("blis")["catalog"]["commit"]

    def problems(self, **locks):
        facts: dict = {}
        return self.N.NousAdapter.lockfile_problems(locks, facts), facts

    def test_locked_binary_and_catalog_pass(self):
        out, facts = self.problems(blis_binary_sha256="sha256:" + self.binary["sha256"],
                                   catalog_commit=self.catalog)
        self.assertEqual(out, [])
        self.assertEqual(facts["lock_binary_id"], "stock")

    def test_unlocked_binary_is_refused(self):
        out, _ = self.problems(blis_binary_sha256="ab" * 32, catalog_commit=self.catalog)
        self.assertTrue(any("not a BLIS binary in references/upstreams/lock.json" in p
                            for p in out), out)

    def test_unlocked_catalog_is_refused(self):
        out, _ = self.problems(blis_binary_sha256=self.binary["sha256"],
                               catalog_commit="1" * 40)
        self.assertTrue(any("not the catalog commit" in p for p in out), out)


class FactValuesAreNotRestated(unittest.TestCase):
    """No shipped file restates an upstreams fact's value as a literal.

    The structural gates above (LINE_CITE, no locked commits) are blind to
    this drift shape: a restated enum or identifier cites nothing and pins
    no version, so it passes both scans while a lock bump that changes the
    fact leaves the restatement silently divergent. Three instances of the
    class are recorded on issue #78 (contracts.py's task_type enum, the
    spotlights outcomes spelled at six sites, gitstate.py's worktree_dir);
    this scan generalizes the per-instance coupling tests those fixes
    carried so a fourth instance fails the moment it lands.
    """

    SCAN = NoRestatedUpstreamFacts.SCAN
    # A fact value counts as restatable when it is identifier-shaped and
    # long enough that an accidental match is implausible.
    VALUE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.@/-]{3,}$")
    # Values the runtime owns independently of any upstream: they appear in
    # the runtime's own state enums (supervisor.py, contracts.py,
    # results.py) and in OTLP attribute names, so their presence in code is
    # not a restatement of the fact that happens to share them. A fact whose
    # values are all in here is skipped; a pair touching one is skipped.
    RUNTIME_OWNED = frozenset({
        "completed", "failed", "cancelled", "rejected", "not_launched",
        "running", "spawning", "reserved", "exited", "recovery_required",
        "accepted", "not_run", "collected", "unavailable", "passed",
        "name", "description", "other", "supported", "inconclusive",
    })
    # Adapter policy and contract keyset declarations: MATERIAL_FIELDS
    # names the request semantics keys the ADAPTER treats as material
    # (base.py:62-66), and contracts.py's <validator>.keys((...)) calls
    # declare the runtime's own message-contract keys. Both legitimately
    # name keys some fact also lists (the campaign keys are the co-designed
    # interface between runtime and upstream); neither is a restatement of
    # the upstream's value. Masked out before scanning.
    POLICY_DECL = re.compile(r"MATERIAL_FIELDS\s*=\s*\(.*?\)|"
                             r"NON_MATERIAL_FIELDS\s*=\s*\{.*?\}|"
                             r"\w+\.keys\(.*?\)\)",
                             re.DOTALL)

    def restatable_values(self):
        """[(upstream, key, [values])] for every string or string-list fact."""
        out = []
        for path in sorted((SKILL / "references" / "upstreams").glob("*.json")):
            if path.name == "lock.json":
                continue
            facts = json.loads(path.read_text())["facts"]
            for key, entry in sorted(facts.items()):
                value = entry.get("value") if isinstance(entry, dict) else entry
                values = value if isinstance(value, list) else [value]
                values = [v for v in values
                          if isinstance(v, str) and self.VALUE_RE.match(v)
                          and v not in self.RUNTIME_OWNED]
                if len(values) >= 2:
                    out.append((path.stem, key, values))
        return out

    def test_no_scanned_file_restates_two_fact_values(self):
        # Two or more of one fact's values spelled adjacently in a literal
        # is the restated-enum shape (a single value can be an ordinary
        # word; the pair is the signature). references/upstreams/ itself is
        # exempt — it is where the values live.
        for upstream, key, values in self.restatable_values():
            # both orderings: a restatement can spell the pair either way
            pairs = [re.escape(a) + r'["\'][^)\n]{0,60}["\']' + re.escape(b)
                     for a in values for b in values if a != b]
            for path in self.SCAN:
                text = self.POLICY_DECL.sub(" ", path.read_text())
                for pat in pairs:
                    with self.subTest(file=str(path.relative_to(ROOT)),
                                      fact=f"{upstream}.{key}"):
                        self.assertEqual(
                            re.findall(pat, text), [],
                            f"restates the {upstream}.{key} fact; read "
                            f"upstreams.fact('{upstream}', '{key}') instead")

    def test_every_fact_sourced_constant_is_used(self):
        # A module-level `X = ...U.fact(...)` that nothing reads again is
        # decorative fact-sourcing: the value is loaded but the code still
        # spells literals (the dead-OUTCOMES shape recorded on issue #78).
        # Uses are counted from the AST in Load context, never from name
        # matches: a same-named constant in another module (the runtime's
        # own OUTCOMES in contracts.py) is a different object and must not
        # launder the dead one. Cross-module credit requires an explicit
        # `from <defining module> import X` (as NAME or NAME as ALIAS) and
        # then counts only the imported alias's loads.
        import ast

        def names_fact(node):
            return (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "fact"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "U")

        modules = sorted(p for p in (SKILL / "scripts").rglob("*.py")
                         if "__pycache__" not in str(p))
        trees = {p: ast.parse(p.read_text()) for p in modules}
        # module name -> the constants it sources from facts
        defined = {}
        for p, tree in trees.items():
            for node in tree.body:
                if (isinstance(node, ast.Assign) and len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Name)
                        and any(names_fact(x) for x in ast.walk(node.value))):
                    defined.setdefault(p.stem, {})[node.targets[0].id] = 0
        self.assertTrue(defined, "no U.fact() constant found; the scan is inert")
        # count genuine reads (Load context) in the defining module...
        for p, tree in trees.items():
            for name in defined.get(p.stem, {}):
                defined[p.stem][name] = sum(
                    isinstance(n, ast.Name) and n.id == name
                    and isinstance(n.ctx, ast.Load) for n in ast.walk(tree))
        # ...and explicit from-imports in other modules
        for p, tree in trees.items():
            for node in ast.walk(tree):
                if not (isinstance(node, ast.ImportFrom) and node.module
                        and node.module.split(".")[-1] in defined):
                    continue
                table = defined[node.module.split(".")[-1]]
                for a in node.names:
                    if a.name in table:
                        alias = a.asname or a.name
                        table[a.name] += sum(
                            isinstance(n, ast.Name) and n.id == alias
                            and isinstance(n.ctx, ast.Load)
                            for n in ast.walk(tree))
        for stem, table in defined.items():
            for name, uses in table.items():
                with self.subTest(constant=name, module=stem):
                    self.assertGreater(
                        uses, 0,
                        f"{stem}.py: {name} is loaded from a fact but never "
                        "used; either consume it or the code is restating "
                        "the fact as literals")


if __name__ == "__main__":
    unittest.main()
