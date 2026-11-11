"""Tests for scripts/check_skill_spec.py — the Agent Skills spec gate.

The checker runs in CI's `spec` job, but only against this repo's own (valid)
skill, so every rejection branch was dead code: 0 of 126 statements executed
under any test, and a regression in the checker would silently stop gating
anything. These tests import the module and drive `check()` with throwaway
skill directories in a tempdir, one case per rejection rule, plus the
PyYAML-vs-fallback parser pair: both must accept the same frontmatter shapes,
because CI's unit matrix includes Python 3.10 hosts where PyYAML may be
missing and only the fallback runs.

Run from the repo root:  python3 -m unittest tests.test_check_skill_spec -v
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check_skill_spec.py"
SKILL = REPO / "skills" / "investigation"

sys.path.insert(0, str(REPO / "scripts"))
import check_skill_spec as css  # noqa: E402

try:
    import yaml  # noqa: F401
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


def skill_md(name: str, description: str = "d", extra: str = "") -> str:
    """A minimal valid frontmatter, with `extra` appended verbatim."""
    return f"---\nname: {name}\ndescription: {description}\n{extra}---\n\n# body\n"


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)

    def write(self, folder: str, text: str) -> Path:
        """A skill dir named `folder` holding SKILL.md `text`; returns the dir.
        Reuses the folder across subTests (idempotent mkdir)."""
        d = self.root / folder
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(text, encoding="utf-8")
        return d

    def check(self, folder: str, text: str) -> list[str]:
        return css.check(self.write(folder, text))


class ValidSkill(Base):
    def test_minimal_skill_passes(self):
        self.assertEqual(self.check("my-skill", skill_md("my-skill")), [])

    def test_all_allowed_fields_pass(self):
        text = ("---\nname: my-skill\ndescription: d\nlicense: Apache-2.0\n"
                "compatibility: any agent\nallowed-tools: Bash, Read\n"
                "metadata:\n  version: \"2.0.0\"\n---\n\nbody\n")
        self.assertEqual(self.check("my-skill", text), [])


class MissingOrMalformed(Base):
    def test_missing_skill_md(self):
        d = self.root / "empty"
        d.mkdir()
        self.assertEqual(css.check(d), [f"missing {d / 'SKILL.md'}"])

    def test_no_frontmatter(self):
        errs = self.check("my-skill", "# just a heading\n")
        self.assertTrue(any("no YAML frontmatter" in e for e in errs))

    def test_unterminated_frontmatter(self):
        errs = self.check("my-skill", "---\nname: my-skill\n")
        self.assertTrue(any("no YAML frontmatter" in e for e in errs))

    def test_unparseable_frontmatter(self):
        # A line that is neither key: value, comment nor blank.
        errs = self.check("my-skill", "---\nname: my-skill\n??? junk\n---\n")
        self.assertTrue(any("frontmatter does not parse" in e for e in errs))

    @unittest.skipUnless(HAVE_YAML, "the fallback parser rejects lists differently")
    def test_frontmatter_that_is_not_a_mapping(self):
        # PyYAML parses this to a list, not a dict.
        errs = self.check("my-skill", "---\n- a\n- b\n---\n")
        self.assertTrue(any("frontmatter does not parse" in e for e in errs))
        self.assertTrue(any("not a mapping" in e for e in errs))


class WithoutPyYAML(Base):
    """The fallback parser path: CI's unit matrix includes py3.10 hosts where
    PyYAML may be absent, so check() must still validate via _fallback_yaml."""

    def run_without_yaml(self, text: str):
        """check() in a subprocess with the yaml import blocked. The errors
        are printed as JSON so no eval is needed."""
        code = ("import sys, json\n"
                "class Block:\n"
                "    def find_spec(self, name, path=None, target=None):\n"
                "        if name == 'yaml' or name.startswith('yaml.'):\n"
                "            raise ImportError('blocked')\n"
                "sys.meta_path.insert(0, Block())\n"
                "sys.path.insert(0, %r)\n"
                "import check_skill_spec as css\n"
                "d = __import__('pathlib').Path(sys.argv[1])\n"
                "print(json.dumps(css.check(d)))\n"
                % str(REPO / "scripts"))
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "my-skill"
            d.mkdir()
            (d / "SKILL.md").write_text(text)
            r = subprocess.run([sys.executable, "-c", code, str(d)],
                               capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_valid_skill_passes_without_pyyaml(self):
        text = ("---\nname: my-skill\ndescription: d\n"
                "metadata:\n  version: \"2.0.0\"\n---\n\nbody\n")
        self.assertEqual(self.run_without_yaml(text), [])

    def test_bad_name_caught_without_pyyaml(self):
        errs = self.run_without_yaml(skill_md("My-Skill"))
        self.assertTrue(any("must be lowercase a-z, 0-9" in e for e in errs))


class FieldRules(Base):
    def test_unexpected_field(self):
        errs = self.check("my-skill", skill_md("my-skill", extra="surprise: 1\n"))
        self.assertTrue(any("unexpected frontmatter fields: ['surprise']" in e for e in errs))

    def test_name_required(self):
        errs = self.check("my-skill", "---\ndescription: d\n---\n")
        self.assertTrue(any("'name' is required" in e for e in errs))

    def test_name_must_match_folder(self):
        errs = self.check("other-name", skill_md("my-skill"))
        self.assertTrue(any("must match the folder name 'other-name'" in e for e in errs))

    def test_name_bad_characters(self):
        for bad in ("My-Skill", "my skill", "-my-skill", "my-skill-", "my--skill"):
            with self.subTest(name=bad):
                # The folder is always a valid name; only `name:` is bad.
                errs = self.check("my-skill", skill_md(bad))
                self.assertTrue(any("must be lowercase a-z, 0-9 and single hyphens" in e
                                    for e in errs), bad)

    def test_name_too_long(self):
        errs = self.check("x" * 65, skill_md("x" * 65))
        self.assertTrue(any("'name' is 65 chars; max 64" in e for e in errs))

    def test_description_required(self):
        errs = self.check("my-skill", "---\nname: my-skill\n---\n")
        self.assertTrue(any("'description' is required" in e for e in errs))

    def test_description_too_long(self):
        errs = self.check("my-skill", skill_md("my-skill", description="x" * 1025))
        self.assertTrue(any("'description' is 1025 chars; max 1024" in e for e in errs))

    def test_license_must_be_a_string(self):
        errs = self.check("my-skill", skill_md("my-skill", extra="license: 7\n"))
        self.assertTrue(any("'license' must be a non-empty string" in e for e in errs))

    def test_license_empty_string(self):
        errs = self.check("my-skill", skill_md("my-skill", extra='license: ""\n'))
        self.assertTrue(any("'license' must be a non-empty string" in e for e in errs))

    def test_compatibility_too_long(self):
        errs = self.check("my-skill", skill_md("my-skill", extra="compatibility: {}\n"
                                                .format("x" * 501)))
        self.assertTrue(any("'compatibility' is 501 chars; max 500" in e for e in errs))

    def test_compatibility_non_string(self):
        errs = self.check("my-skill", skill_md("my-skill", extra="compatibility: 7\n"))
        self.assertTrue(any("'compatibility' must be a non-empty string" in e
                            for e in errs))

    @unittest.skipUnless(HAVE_YAML, "the fallback parser reads every value as a string")
    def test_metadata_non_string_value(self):
        # Unquoted number under PyYAML: the exact trap the error message names.
        # (Only PyYAML produces a non-string scalar; _fallback_yaml always
        # yields strings, so it can never trip this branch.)
        errs = self.check("my-skill", skill_md("my-skill",
                                               extra="metadata:\n  version: 7\n"))
        self.assertTrue(any("must map strings to strings; 'version': 7" in e
                            for e in errs))

    def test_metadata_not_a_map(self):
        errs = self.check("my-skill", skill_md("my-skill", extra="metadata: 7\n"))
        self.assertTrue(any("'metadata' must be a map" in e for e in errs))

    def test_allowed_tools_must_be_a_string(self):
        errs = self.check("my-skill", skill_md("my-skill",
                                               extra="allowed-tools:\n  - Bash\n"))
        self.assertTrue(any("'allowed-tools' must be a space-separated string" in e
                            for e in errs))

    def test_line_limit(self):
        # skill_md(...) is 6 lines; MAX_LINES more pushes it over the limit.
        text = skill_md("my-skill") + ("filler\n" * css.MAX_LINES)
        errs = self.check("my-skill", text)
        self.assertTrue(any(f"has {css.MAX_LINES + 6} lines; the spec says keep it "
                            f"under {css.MAX_LINES}" in e for e in errs))


class FallbackParser(unittest.TestCase):
    """_fallback_yaml: the parser used when PyYAML is missing (py3.10 hosts)."""

    def parse(self, src: str) -> dict:
        return css._fallback_yaml(src)

    def test_flat_keys_and_quoting(self):
        self.assertEqual(self.parse('name: my-skill\ndescription: "d d"\n'),
                         {"name": "my-skill", "description": "d d"})

    def test_folded_and_literal_block_scalars(self):
        folded = self.parse("description: >-\n  one\n  two\n")
        self.assertEqual(folded["description"], "one two")
        literal = self.parse("description: |-\n  one\n  two\n")
        self.assertEqual(literal["description"], "one\ntwo")

    def test_one_level_map(self):
        out = self.parse("metadata:\n  version: \"2.0.0\"\n  runtime-version: '0.1.0'\n")
        self.assertEqual(out, {"metadata": {"version": "2.0.0",
                                            "runtime-version": "0.1.0"}})

    def test_map_skips_blank_block_lines(self):
        # Blank lines inside a block are skipped, not parsed as map entries.
        out = self.parse("metadata:\n\n  version: \"1\"\n")
        self.assertEqual(out, {"metadata": {"version": "1"}})

    def test_unparseable_map_line_raises(self):
        with self.assertRaises(ValueError):
            self.parse("metadata:\n  ??? junk\n")

    def test_comments_and_blank_lines_skipped(self):
        out = self.parse("# comment\n\nname: my-skill\n")
        self.assertEqual(out, {"name": "my-skill"})

    def test_unparseable_line_raises(self):
        with self.assertRaises(ValueError):
            self.parse("??? junk\n")

    @unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
    def test_fallback_agrees_with_pyyaml_on_the_shipped_skill(self):
        """The shipped frontmatter parses the same both ways: CI's spec job
        runs with PyYAML, a 3.10 host without it runs the fallback."""
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        fm_src = css.split_frontmatter(text)[0]
        import yaml
        self.assertEqual(css._fallback_yaml(fm_src), yaml.safe_load(fm_src))


class ShippedSkill(unittest.TestCase):
    def test_the_shipped_skill_is_valid(self):
        self.assertEqual(css.check(SKILL), [])

    def test_cli_exit_codes(self):
        ok = subprocess.run([sys.executable, str(SCRIPT), str(SKILL)],
                            capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("OK", ok.stdout)
        bad = subprocess.run([sys.executable, str(SCRIPT)],
                             capture_output=True, text=True)
        self.assertEqual(bad.returncode, 2)  # usage: missing the dir argument
        self.assertIn("Usage", bad.stderr)

    def test_cli_reports_a_broken_skill_and_exits_1(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "bad"
            d.mkdir()
            (d / "SKILL.md").write_text("# no frontmatter\n")
            r = subprocess.run([sys.executable, str(SCRIPT), str(d)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 1)
            self.assertIn("FAIL", r.stdout)
            self.assertIn("no YAML frontmatter", r.stdout)


if __name__ == "__main__":
    unittest.main()
