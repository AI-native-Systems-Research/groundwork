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
        for name in ("nous", "spotlights"):
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


if __name__ == "__main__":
    unittest.main()
