"""R8: coordinator docs and the no-chaining rule (plan §4.7, §5 R8; spec §12, §14).

1. No SKILL.md / reference / asset text tells the agent to launch a migrated
   engine (Nous, Spotlights static) directly. The only place those command
   lines may appear as commands is runtime-protocol.md's "What the adapter
   runs" section. Legacy direct launches still documented under owner
   decision D10 are in LEGACY_ALLOWLIST; B2 empties it.
2. Stage 05 of signal-pipeline is never instructed.
3. Spec §14: a Spotlights result leads to stopping, clarification or a
   separately justified and approved Nous request; nothing chains. Checked in
   the docs, in the runtime's code (no result/recovery/export/adapter code
   path calls launch, reserve, prepare or submit), and by building a real
   Spotlights static result and checking it created nothing.
"""
import ast
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runtime_util import SCRIPTS  # noqa: E402

SKILL = SCRIPTS.parent
PROTOCOL = SKILL / "references" / "runtime-protocol.md"
ADAPTER_SECTION = "## What the adapter runs"
RUNTIME_PKG = SCRIPTS / "ains_runtime"

# Engine commands the runtime governs (Milestone A). Never a command in the
# docs outside the adapter section.
MIGRATED = ("nous run", "nous resume", "nous stop", "spotlights-engine")
# Engine commands not yet governed. Each may appear as a command only in the
# files listed here.
LEGACY = ("signal-pipeline", "capture_prom.py")
LEGACY_ALLOWLIST = {
    # D10: the telemetry-driven Spotlights path stays on its legacy per-launch
    # go until R6a/R6 lift the hold. Labelled "legacy gate, not
    # runtime-enforced" in the file.
    ("references/spotlights.md", "signal-pipeline"),
    # Telemetry capture is governed only in Milestone B (B1). Legacy go.
    ("references/telemetry.md", "capture_prom.py"),
}
NEGATION = re.compile(r"\b(never|not|no|don't|do not|only through|only via)\b", re.I)
STAGE05 = re.compile(r"(--(to|only|from)-stage[ =]0?5\b|--inject[ =]0?5|run_pipeline)")


def doc_files() -> list[Path]:
    files = [SKILL / "SKILL.md", *sorted((SKILL / "references").glob("*.md"))]
    files += sorted(p for p in (SKILL / "assets").rglob("*") if p.is_file())
    return files


def strip_adapter_section(path: Path, text: str) -> str:
    if path != PROTOCOL:
        return text
    i = text.find(ADAPTER_SECTION)
    if i < 0:
        return text
    j = text.find("\n## ", i + len(ADAPTER_SECTION))
    return text[:i] + (text[j:] if j >= 0 else "")


def code_blocks(text: str) -> list[str]:
    """Logical lines of fenced code blocks (backslash continuations joined)."""
    out, inside, buf = [], False, ""
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            inside = not inside
            continue
        if inside:
            buf += line.rstrip()
            if buf.endswith("\\"):
                buf = buf[:-1] + " "
                continue
            out.append(buf)
            buf = ""
    return out


def command_of(line: str) -> str:
    """The command a shell line runs: env assignments and `python3 path/`
    prefixes skipped, e.g. 'NOUS_X=y nous run a' -> 'nous run a'."""
    toks = line.split("#", 1)[0].split()
    while toks and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=\S*", toks[0]):
        toks.pop(0)
    if toks and toks[0] in ("python3", "python", "exec", "nohup"):
        toks.pop(0)
    if toks:
        toks[0] = toks[0].rsplit("/", 1)[-1].strip("\"'")
    return " ".join(toks)


def mentions(text: str, rx: re.Pattern) -> list[tuple[str, str]]:
    """(context, line) for every prose line matching rx. The context is the
    line with two lines either side, plus the nearest heading above, so a
    list item under "## What this skill will never do" counts as negated."""
    lines = text.splitlines()
    out, inside, heading = [], False, ""
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            inside = not inside
            continue
        if line.startswith("#"):
            heading = line
        if not inside and rx.search(line):
            out.append((heading + "\n" + "\n".join(lines[max(0, i - 2):i + 3]), line))
    return out


class NoDirectLaunches(unittest.TestCase):
    def test_protocol_has_the_adapter_section(self):
        text = PROTOCOL.read_text()
        self.assertIn(ADAPTER_SECTION, text)
        section = text[text.index(ADAPTER_SECTION):]
        self.assertIn("nous run", section)
        self.assertIn("spotlights-engine", section)
        self.assertRegex(section, r"[Nn]ever run them yourself")

    def test_no_migrated_engine_command_outside_the_adapter_section(self):
        for path in doc_files():
            rel = path.relative_to(SKILL).as_posix()
            text = strip_adapter_section(path, path.read_text(errors="replace"))
            for line in code_blocks(text):
                cmd = command_of(line)
                for eng in MIGRATED:
                    with self.subTest(file=rel, line=line):
                        self.assertFalse(cmd == eng or cmd.startswith(eng + " "),
                                         f"{rel}: direct `{eng}` command: {line!r}")

    def test_inline_mentions_of_migrated_launches_are_prohibitions(self):
        # Prose may name `nous run` etc. only to forbid or describe it.
        rx = re.compile(r"`(nous (run|resume)\b[^`]*|spotlights-engine(\s[^`]*)?)`")
        for path in doc_files():
            rel = path.relative_to(SKILL).as_posix()
            text = strip_adapter_section(path, path.read_text(errors="replace"))
            for ctx, line in mentions(text, rx):
                with self.subTest(file=rel, line=line):
                    self.assertRegex(ctx, NEGATION, f"{rel}: names a migrated launch "
                                                    f"without forbidding it: {line!r}")

    def test_legacy_commands_only_where_allowlisted_and_labelled(self):
        seen = set()
        for path in doc_files():
            rel = path.relative_to(SKILL).as_posix()
            text = path.read_text(errors="replace")
            for line in code_blocks(text):
                cmd = command_of(line)
                for eng in LEGACY:
                    if cmd == eng or cmd.startswith(eng + " "):
                        seen.add((rel, eng))
                        with self.subTest(file=rel, line=line):
                            self.assertIn((rel, eng), LEGACY_ALLOWLIST,
                                          f"{rel}: legacy direct `{eng}` not allowlisted")
        for rel, _ in LEGACY_ALLOWLIST:
            self.assertRegex((SKILL / rel).read_text(),
                             r"[Ll]egacy gate, not runtime-enforced",
                             f"{rel} must label its legacy launch")
        # A stale allowlist entry hides nothing; drop it when its launch goes.
        self.assertEqual(seen, LEGACY_ALLOWLIST)

    def test_skill_md_forbids_direct_launches_and_labels_legacy(self):
        t = (SKILL / "SKILL.md").read_text()
        self.assertIn("scripts/runtime.py", t)
        self.assertRegex(t, r"Never call `nous run`/`nous resume` or `spotlights-engine`")
        self.assertRegex(t, r"[Ll]egacy gate, not\s+runtime-enforced")
        self.assertRegex(t, r"quoting the owner's reply and the\s+card's short code")
        self.assertIn("runtime-version:", t.split("\n---", 1)[0])


class NoStage05(unittest.TestCase):
    def test_stage_05_is_never_instructed(self):
        for path in doc_files():
            rel = path.relative_to(SKILL).as_posix()
            text = path.read_text(errors="replace")
            for line in code_blocks(text):
                with self.subTest(file=rel, line=line):
                    self.assertIsNone(STAGE05.search(line), f"{rel}: {line!r}")
            for ctx, line in mentions(text, re.compile(STAGE05.pattern + r"|stage 05",
                                                       re.I)):
                with self.subTest(file=rel, line=line):
                    self.assertRegex(ctx, NEGATION, f"{rel}: {line!r}")

    def test_signal_pipeline_command_stops_at_04(self):
        text = (SKILL / "references" / "spotlights.md").read_text()
        cmds = [ln for ln in code_blocks(text) if command_of(ln).startswith("signal-pipeline")]
        self.assertTrue(cmds)
        for c in cmds:
            self.assertIn("--to-stage 04", c)


class NoAutoChainingDocs(unittest.TestCase):
    def test_docs_say_stop_clarify_or_a_separately_approved_request(self):
        skill = re.sub(r"\s+", " ", (SKILL / "SKILL.md").read_text())
        self.assertIn("Nothing chains automatically", skill)
        self.assertIn("stopping needs no launch", skill)
        self.assertIn("own card and go", skill)

        proto = re.sub(r"\s+", " ", PROTOCOL.read_text())
        for s in ("**Stop.**", "Stopping needs no launch", "**Clarify.**",
                  "**Propose one next step.**", "Never chain automatically",
                  "a result never starts anything"):
            self.assertIn(s, proto)
        self.assertRegex(proto, r"Spotlights candidate becomes a Nous request .* its own "
                                r"card and its own go")

        spot = re.sub(r"\s+", " ", (SKILL / "references" / "spotlights.md").read_text())
        self.assertIn("A Spotlights result never starts a Nous run by itself", spot)
        self.assertRegex(spot, r"stopping, or asking the owner, are equally valid")

        tmpl = (SKILL / "assets" / "plan.md.tmpl").read_text()
        self.assertIn("## Step log", tmpl)
        self.assertIn("next_step", tmpl)
        self.assertRegex(re.sub(r"\s+", " ", tmpl), r"stopping needs no launch")

    def test_plan_approval_grants_no_launch(self):
        for rel in ("SKILL.md", "references/method-plan.md", "assets/plan.md.tmpl"):
            t = re.sub(r"\s+", " ", (SKILL / rel).read_text())
            self.assertRegex(t, r"(Approving it grants no launch|approving it grants no "
                                r"launch|Approval of the plan is not a go)", rel)


# --- code: nothing in the runtime launches from a result -----------------------------

FORBIDDEN_CALLS = {"launch", "reserve", "reserve_invocation", "spawn_supervisor",
                   "supervise", "prepare", "submit", "approve"}
# Modules on the result / status / recovery / export / adapter paths. None may
# start, reserve, prepare, approve or submit anything.
NO_CHAIN_MODULES = ("results.py", "recovery.py", "export.py", "limits.py",
                    "artifacts.py", "adapters/base.py", "adapters/__init__.py",
                    "adapters/nous.py", "adapters/spotlights_static.py")


def called_names(path: Path) -> set[str]:
    out = set()
    for n in ast.walk(ast.parse(path.read_text())):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def imported_names(path: Path) -> set[str]:
    out = set()
    for n in ast.walk(ast.parse(path.read_text())):
        if isinstance(n, ast.ImportFrom):
            out |= {a.name for a in n.names}
            out.add(n.module or "")
    return out


class NoAutoChainingCode(unittest.TestCase):
    def test_result_paths_never_call_launch_reserve_prepare_or_submit(self):
        for rel in NO_CHAIN_MODULES:
            path = RUNTIME_PKG / rel
            with self.subTest(module=rel):
                self.assertTrue(path.is_file(), rel)
                bad = called_names(path) & FORBIDDEN_CALLS
                self.assertEqual(bad, set(), f"{rel} calls {sorted(bad)}")
                bad = imported_names(path) & (FORBIDDEN_CALLS | {"revalidate"})
                self.assertEqual(bad, set(), f"{rel} imports {sorted(bad)}")

    def test_result_paths_import_nothing_that_starts_work(self):
        # From preparation they may take helpers (paths, context, errors), never
        # prepare/revalidate; from supervisor, constants and helpers, never launch.
        allowed = {"preparation": {"_context", "runtime_dir", "PrepareError"}}
        for rel in NO_CHAIN_MODULES:
            for n in ast.walk(ast.parse((RUNTIME_PKG / rel).read_text())):
                if isinstance(n, ast.ImportFrom):
                    mod = (n.module or "").rsplit(".", 1)[-1]
                    if mod in allowed:
                        with self.subTest(module=rel, imports=mod):
                            self.assertLessEqual({a.name for a in n.names}, allowed[mod])

    def test_cli_collect_and_result_handlers_never_launch(self):
        from ains_runtime import cli
        import inspect
        import textwrap
        for fn in (cli.cmd_collect, cli.cmd_result, cli.cmd_status, cli.cmd_recover):
            names = set()
            for n in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(fn)))):
                if isinstance(n, ast.Name):
                    names.add(n.id)
                elif isinstance(n, ast.Attribute):
                    names.add(n.attr)
                elif isinstance(n, ast.alias):
                    names.add(n.name)
            with self.subTest(fn=fn.__name__):
                self.assertEqual(names & FORBIDDEN_CALLS, set())


class RequestExamplesPassAdapterChecks(unittest.TestCase):
    """The shipped request examples are in the shapes R4/R5 accept (semantics,
    representability and material policy; the host-dependent checks such as
    engine sha and apparatus are R4/R5's own tests)."""

    def load(self, name):
        import json
        from _runtime_util import EXAMPLES
        return json.loads((EXAMPLES / name).read_text())

    def test_nous_example(self):
        from ains_runtime.adapters import nous as N
        from ains_runtime.contracts import validate_message
        m = self.load("experiment-request.json")
        self.assertEqual(m["body"]["capability"], "nous")
        self.assertEqual(validate_message(m, {}), [])
        a = N.NousAdapter()
        self.assertEqual(a.check_semantics(m), [])
        self.assertEqual(N.NousAdapter.semantics_problems(m), [])
        self.assertEqual(N.NousAdapter.representability_problems(m), [])
        for k in N.REQUIRED_LOCKS:
            self.assertTrue(m["body"]["locks"].get(k), k)

    def test_spotlights_static_example(self):
        from ains_runtime.adapters import spotlights_static as SS
        from ains_runtime.contracts import validate_message
        m = self.load("experiment-request-next-step.json")
        self.assertEqual(m["body"]["capability"], "spotlights-static")
        self.assertEqual(validate_message(m, {}), [])
        a = SS.SpotlightsStaticAdapter()
        self.assertEqual(a.check_semantics(m), [])
        self.assertEqual(a._semantic_problems(m["body"]), [])
        self.assertTrue(m["body"]["next_step"])


import test_runtime_results as R7  # noqa: E402
from test_runtime_spotlights_static import Base as SpotBase  # noqa: E402


class SpotlightsResultLaunchesNothing(R7.Mixin, SpotBase):
    """A Spotlights static result with candidates creates no new message, work
    item, preparation, approval or invocation: the next step (stop, clarify,
    or a Nous request) is the coordinator's, with its own card and go."""

    def setUp(self):
        super().setUp()
        self.study = self.repo / "investigations" / "s1"
        self.study.mkdir(parents=True)

    def counts(self):
        return (len(self.store.list_messages()), len(self.store.list_work()),
                len(self.store.list_preparations()), len(self.store.list_approvals()),
                len(self.store.list_invocations()))

    def test_candidates_result_creates_nothing_next(self):
        prep = self.prepare()
        self.place(self.record(prep), "candidates-found")
        inv = self.invoke(prep)
        before = self.counts()
        b, _ = self.build(inv)  # R7's NoLaunch guard: no supervisor or engine starts
        self.assertEqual(b["execution"]["state"], "completed")
        self.assertTrue(b["findings"]["candidates"])
        self.assertEqual(self.counts(), before)
        caps = {m["envelope"]["body"].get("capability")
                for m in self.store.list_messages()
                if m["envelope"]["schema"].startswith("ains.experiment-request/")}
        self.assertEqual(caps, {"spotlights-static"})


if __name__ == "__main__":
    unittest.main()
