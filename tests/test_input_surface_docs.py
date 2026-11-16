"""Issue #59's gate: every shipped user-facing input surface is in a reference doc.

The runtime's contract with an agent following the shipped docs is that each
script flag, documented env knob, runtime subcommand and adapter semantics key
is discoverable in references/ before it is needed. Nothing else compared the
code's input surface to those docs (test_runtime_docs.py pins launch-command
prose; test_upstreams.py pins version facts), so a knob could ship while its
governing doc stayed silent and the agent hit a hard error whose remedy was
undiscoverable (issue #59's five instances).

Three surfaces are pinned, each against the doc that governs it:

1. capture_prom.py: every build_parser() flag and every INVESTIGATION_* env
   read must appear in references/telemetry.md.
2. runtime.py: every public COMMANDS entry must appear in
   references/runtime-protocol.md as a command (`RT <name>` or
   `runtime.py <name>`), not merely as an English word (show/export shipped
   with zero mentions — instance 4).
3. adapters: every MATERIAL_FIELDS / NON_MATERIAL_FIELDS semantics key must
   be named in runtime-protocol.md's capability table, and the material
   `secrets` key must be explained in prose, not just listed (instance 3:
   its pass-through, material-presence and refusal semantics existed only
   in adapter docstrings).

The assertions are on names and command forms, not prose quality; describing
each key well is the doc's own job, checked by reading it.
"""
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills/investigation/scripts"
SKILL = SCRIPTS.parent
REFERENCES = SKILL / "references"

sys.path.insert(0, str(SCRIPTS))

# argparse adds -h/--help to every parser; it is not a surface the doc owes
# a mention to.
BUILTIN_FLAGS = {"-h", "--help"}


def doc_text(name: str) -> str:
    return (REFERENCES / name).read_text()


def prose_lines(text: str) -> list[str]:
    """Lines that are neither table rows nor headings nor code blocks."""
    out, inside = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            inside = not inside
            continue
        if not inside and not line.lstrip().startswith(("|", "#")):
            out.append(line)
    return out


class CapturePromSurface(unittest.TestCase):
    """telemetry.md documents every flag and env knob capture_prom.py reads."""

    @classmethod
    def setUpClass(cls):
        import capture_prom
        cls.doc = doc_text("telemetry.md")
        cls.parser = capture_prom.build_parser()
        cls.source = (SCRIPTS / "capture_prom.py").read_text()

    def test_every_flag_is_in_telemetry_md(self):
        flags = sorted(o for a in self.parser._actions
                       for o in a.option_strings if o not in BUILTIN_FLAGS)
        self.assertTrue(flags)
        for flag in flags:
            with self.subTest(flag=flag):
                self.assertIn(flag, self.doc,
                              f"capture_prom.py accepts {flag} but telemetry.md "
                              "never mentions it")

    def test_every_investigation_env_read_is_in_telemetry_md(self):
        reads = sorted(set(
            re.findall(r'os\.environ(?:\.get)?\(\s*"((?:INVESTIGATION_)[A-Z_]+)"',
                       self.source)
            + re.findall(r'os\.environ\[\s*"((?:INVESTIGATION_)[A-Z_]+)"\]',
                         self.source)))
        self.assertTrue(reads)
        for name in reads:
            with self.subTest(env=name):
                self.assertIn(f"${name}", self.doc,
                              f"capture_prom.py reads ${name} but telemetry.md "
                              "never mentions it")

    def test_the_cleartext_http_refusal_names_its_override(self):
        # Instance 2: the plain-http refusal and --allow-insecure-http shipped
        # (PR #31) while telemetry.md documented neither. The refusal names
        # the flag at the point of failure; the doc must name it beforehand
        # (synopsis, url-source row or exit-code list).
        self.assertIn("--allow-insecure-http", self.doc)


class RuntimeCommandSurface(unittest.TestCase):
    """runtime-protocol.md names every public runtime.py subcommand as one."""

    @classmethod
    def setUpClass(cls):
        from ains_runtime import cli
        cls.doc = doc_text("runtime-protocol.md")
        cls.commands = [name for name, *_ in cli.COMMANDS]

    def test_every_public_subcommand_is_in_runtime_protocol_md(self):
        # Instance 4: `show` and `export` shipped with zero mentions. The
        # approval card itself points at `runtime.py show PID --json`
        # (approval.py), so a user hitting the card's pointer found nothing.
        # The command form is required: `show`/`export` also occur as English
        # words ("shows", "exports"), which is not documentation of a command.
        self.assertTrue(self.commands)
        for name in self.commands:
            with self.subTest(command=name):
                self.assertRegex(
                    self.doc, rf"\b(?:RT|runtime\.py) {name}\b",
                    f"runtime.py has a `{name}` subcommand but "
                    "runtime-protocol.md never shows it as a command "
                    "(`RT " + name + " …` or `runtime.py " + name + " …`)")

    def test_private_commands_stay_undocumented_as_commands(self):
        # _supervise is started only by launch; the docs must not present it.
        from ains_runtime import cli
        for name, *_ in getattr(cli, "PRIVATE_COMMANDS", ()):
            self.assertIsNone(
                re.search(rf"\b(?:RT|runtime\.py) {name}\b", self.doc),
                f"runtime-protocol.md documents private `{name}` as a command")


class AdapterSemanticsSurface(unittest.TestCase):
    """The capability table names every declared semantics key, and the
    material `secrets` key is explained in prose, not just listed."""

    @classmethod
    def setUpClass(cls):
        cls.doc = doc_text("runtime-protocol.md")
        # Capability rows look like: | `nous` | <semantics> | <elsewhere> |
        cls.rows = {}
        for line in cls.doc.splitlines():
            m = re.match(r"^\|\s*`([a-z-]+)`\s*\|", line)
            if m:
                cls.rows[m.group(1)] = line

    def capability_row(self, capability):
        row = self.rows.get(capability)
        self.assertIsNotNone(row, f"runtime-protocol.md has no capability row "
                                  f"for `{capability}`")
        return row

    def test_every_declared_semantics_key_is_in_the_capability_table(self):
        from ains_runtime import adapters
        for capability in adapters.names():
            adapter = adapters.get(capability)
            row = self.capability_row(capability)
            keys = sorted(set(adapter.MATERIAL_FIELDS)
                          | set(adapter.NON_MATERIAL_FIELDS))
            self.assertTrue(keys)
            for key in keys:
                with self.subTest(capability=capability, key=key):
                    self.assertIn(key, row,
                                  f"`{capability}` adapter declares semantics "
                                  f"key `{key}` but the capability row in "
                                  "runtime-protocol.md never names it")

    def test_secrets_is_explained_in_prose(self):
        # Instance 3: the capability table names `secrets` in its Optional
        # lists and nothing else in any shipped doc did. The behavior is
        # load-bearing three ways an agent cannot infer (only route for a
        # non-ANTHROPIC_/CLAUDE_ credential to reach the engine; presence is
        # material and part of the approval digest; a presence change since
        # prepare refuses launch), so one prose line outside the table must
        # mention it.
        hits = [ln for ln in prose_lines(self.doc) if "secrets" in ln]
        self.assertTrue(
            hits,
            "`secrets` is named only in the capability table; runtime-protocol"
            ".md never explains it in prose (engine-env pass-through, material"
            " presence, launch refusal on presence change)")


if __name__ == "__main__":
    unittest.main()
