"""Tests for skills/investigation/scripts/verify_upstreams.py.

The bump-time verifier (references/upstreams/README.md steps 3 and 6): the
engine-sha and BLIS binary/catalog checks, the knob-flag checks against
`blis run --help`, the generated-docs rewrite, and the CLI's exit codes.

Nothing real is installed or launched: the machine config is isolated with
INVESTIGATION_CONFIG_HOME -> a tempdir, engine facts come from patched
`doctor.engine_info` responses, and each "BLIS binary" is a tiny shell script
in a tempdir whose bytes hash to the fake lock entry. Catalog checkouts are
throwaway git repos (same pattern as test_doctor.py / test_new_study.py).
"""
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills/investigation/scripts"

sys.path.insert(0, str(SCRIPTS))
import upstreams as U  # noqa: E402
import verify_upstreams as V  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
           "GIT_CONFIG_NOSYSTEM": "1"}

try:
    import yaml  # noqa: F401
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


def sh(*cmd, cwd=None):
    r = subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                        "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *cmd],
                       cwd=cwd, env={**os.environ, **GIT_ENV},
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {cmd} failed: {r.stderr}")
    return r.stdout.strip()


def make_repo(path: Path) -> str:
    """A git repo with one empty commit and an origin remote; returns HEAD."""
    path.mkdir(parents=True, exist_ok=True)
    sh("init", "-q", cwd=path)
    sh("commit", "-q", "--allow-empty", "-m", "init", cwd=path)
    sh("remote", "add", "origin", "https://github.com/org/repo.git", cwd=path)
    return sh("rev-parse", "HEAD", cwd=path)


def write_blis_binary(path: Path, flags: str = "", executable: bool = True) -> str:
    """A fake BLIS binary: `run --help` prints `flags`. Returns its sha256."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\ncat <<'EOF'\n" + flags + "\nEOF\n")
    if executable:
        path.chmod(0o755)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def report() -> dict:
    return {"ok": [], "warnings": [], "stale": [], "failures": [], "notes": []}


@unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
class CheckEngines(unittest.TestCase):
    """check_engines: installed sha vs the lock, per engine (nous, spotlights)."""

    def run_check(self, engine_info) -> dict:
        rep = report()
        with mock.patch.object(U, "entry", return_value={"commit": "c" * 40}), \
             mock.patch.object(V.doctor, "read_pins", return_value={}), \
             mock.patch.object(V.doctor, "engine_info", return_value=engine_info):
            V.check_engines(rep)
        return rep

    def test_not_installed_is_a_warning_not_a_failure(self):
        rep = self.run_check({"found": False, "warnings": []})
        self.assertEqual(rep["failures"], [])
        self.assertTrue(any("nous not installed here" in w for w in rep["warnings"]))
        self.assertTrue(any("spotlights-engine not installed here" in w
                            for w in rep["warnings"]))

    def test_sha_mismatch_is_a_failure(self):
        rep = self.run_check({"found": True, "sha": "d" * 40, "warnings": []})
        self.assertEqual(len(rep["failures"]), 2)  # nous + spotlights
        self.assertIn("nous: installed dddddddddddd is not the locked cccccccccccc",
                      rep["failures"])

    def test_sha_match_is_ok(self):
        rep = self.run_check({"found": True, "sha": "c" * 40, "warnings": []})
        self.assertEqual(rep["failures"], [])
        self.assertTrue(any("nous: installed sha equals the lock" in o for o in rep["ok"]))


@unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
class CheckBlis(unittest.TestCase):
    """check_blis: binary sha256, `run --help` flags, knob flags/prefixes, catalog."""

    def setUp(self):
        self._cfg = tempfile.TemporaryDirectory()
        self.addCleanup(self._cfg.cleanup)
        os.environ["INVESTIGATION_CONFIG_HOME"] = self._cfg.name
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "apparatus"
        self.root.mkdir()

    def write_machine(self, text: str) -> None:
        (Path(self._cfg.name) / "config.yaml").write_text(text)

    def apparatus(self, binaries: dict, catalog_commit: str) -> dict:
        self.write_machine(f"blis_apparatus_root: {self.root}\n")
        return {"binaries": binaries,
                "catalog": {"commit": catalog_commit, "default_dir": "blis-catalog"}}

    def run_check(self, entry: dict, knobs=None) -> dict:
        rep = report()
        with mock.patch.object(U, "entry", return_value=entry), \
             mock.patch.object(U, "fact", return_value=knobs or []):
            V.check_blis(rep)
        return rep

    def test_no_apparatus_root_is_a_warning(self):
        rep = self.run_check({"binaries": {}, "catalog": {"commit": "e" * 40,
                                                          "default_dir": "blis-catalog"}})
        self.assertTrue(any("no blis_apparatus_root" in w for w in rep["warnings"]))
        self.assertEqual(rep["failures"], [])

    def test_missing_binary_is_a_warning(self):
        entry = self.apparatus({"gone": {"file": "bin/gone", "sha256": "0" * 64}}, "e" * 40)
        rep = self.run_check(entry)
        self.assertTrue(any("bin/gone not on this machine" in w for w in rep["warnings"]))
        self.assertEqual(rep["failures"], [])

    def test_sha_mismatch_is_a_failure(self):
        sha = write_blis_binary(self.root / "bin" / "x")
        entry = self.apparatus({"x": {"file": "bin/x", "sha256": "f" * 64}}, "e" * 40)
        rep = self.run_check(entry)
        self.assertTrue(any("sha256" in f and "is not the locked" in f
                            for f in rep["failures"]), rep["failures"])

    def test_help_failure_is_a_failure(self):
        # Bytes hash to the lock, but the file is not executable: spawning it
        # raises OSError, so help_flags returns None ("`run --help` failed").
        noexec = self.root / "bin" / "noexec"
        noexec.parent.mkdir(parents=True, exist_ok=True)
        noexec.write_text("not executable")
        sha = hashlib.sha256(noexec.read_bytes()).hexdigest()
        entry = self.apparatus({"noexec": {"file": "bin/noexec", "sha256": sha}}, "e" * 40)
        rep = self.run_check(entry)
        self.assertIn("blis/noexec: `run --help` failed", rep["failures"])

    def test_matching_binary_and_flags_are_ok(self):
        sha = write_blis_binary(self.root / "bin" / "ok",
                                flags="  --kv-cpu-blocks int\n  --kv-event-delay dur\n")
        entry = self.apparatus({"ok": {"file": "bin/ok", "sha256": sha}}, "e" * 40)
        knobs = [{"label": "KV offload knobs", "flags": ["--kv-cpu-blocks"]}]
        rep = self.run_check(entry, knobs)
        self.assertEqual(rep["failures"], [])
        self.assertEqual(rep["stale"], [])
        self.assertTrue(any("blis/ok: sha256 matches; flags checked" in o
                            for o in rep["ok"]), rep["ok"])

    def test_knob_flag_missing_from_help_is_stale(self):
        sha = write_blis_binary(self.root / "bin" / "ok", flags="  --kv-cpu-blocks int\n")
        entry = self.apparatus({"ok": {"file": "bin/ok", "sha256": sha}}, "e" * 40)
        knobs = [{"label": "KV offload knobs",
                  "flags": ["--kv-cpu-blocks", "--kv-offload-threshold"]}]
        rep = self.run_check(entry, knobs)
        self.assertIn("blis/ok: knob 'KV offload knobs': --kv-offload-threshold is not "
                      "a flag of this binary", rep["stale"])
        self.assertEqual(rep["failures"], [])

    def test_knob_flag_prefix_with_no_match_is_stale(self):
        sha = write_blis_binary(self.root / "bin" / "ok", flags="  --kv-cpu-blocks int\n")
        entry = self.apparatus({"ok": {"file": "bin/ok", "sha256": sha}}, "e" * 40)
        knobs = [{"label": "P", "flag_prefix": "--nope-"}]
        rep = self.run_check(entry, knobs)
        self.assertIn("blis/ok: knob 'P': no --nope-* flag", rep["stale"])

    def test_catalog_not_a_checkout_is_a_warning(self):
        sha = write_blis_binary(self.root / "bin" / "ok")
        entry = self.apparatus({"ok": {"file": "bin/ok", "sha256": sha}}, "e" * 40)
        rep = self.run_check(entry)
        self.assertTrue(any("blis-catalog is not a git checkout here" in w
                            for w in rep["warnings"]))

    def test_catalog_head_equals_and_differs_from_the_lock(self):
        sha = write_blis_binary(self.root / "bin" / "ok")
        head = make_repo(self.root / "blis-catalog")
        # HEAD equals the lock.
        entry = self.apparatus({"ok": {"file": "bin/ok", "sha256": sha}}, head)
        rep = self.run_check(entry)
        self.assertIn("blis catalog: HEAD equals the lock", rep["ok"])
        # HEAD differs from the lock.
        entry = self.apparatus({"ok": {"file": "bin/ok", "sha256": sha}}, "0" * 40)
        rep = self.run_check(entry)
        self.assertTrue(any("blis catalog:" in f and "is at" in f
                            for f in rep["failures"]), rep["failures"])


class CheckNotes(unittest.TestCase):
    def test_verify_note_knobs_are_listed_for_the_owner(self):
        rep = report()
        knobs = [{"label": "L", "verify_note": "check X"},
                 {"label": "M"}]  # no note: not listed
        with mock.patch.object(U, "fact", return_value=knobs):
            V.check_notes(rep)
        self.assertEqual(rep["notes"], ["blis knob 'L': check X"])


class WriteDocs(unittest.TestCase):
    """write_docs: replace the generated table between its mark and the blank
    line; a no-op rewrite returns False (idempotent)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.plan = Path(self._tmp.name) / "method-plan.md"

    def test_rewrites_the_generated_table_and_is_idempotent(self):
        self.plan.write_text("before\n<!-- generated from references/upstreams/"
                             "blis.json -->\nOLD TABLE\n\nafter\n")
        table = "| Representable | Not representable |\n|---|---|\n| one | |"
        with mock.patch.object(V, "METHOD_PLAN", self.plan), \
             mock.patch.object(U, "representability_table_md", return_value=table):
            self.assertTrue(V.write_docs())
            self.assertFalse(V.write_docs())  # second run: nothing to change
        text = self.plan.read_text()
        self.assertIn(table, text)
        self.assertIn("before\n", text)
        self.assertIn("\n\nafter", text)  # the trailing blank line is kept
        self.assertNotIn("OLD TABLE", text)

    def run_main(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = V.main(list(argv))
        return rc, buf.getvalue()

    def test_missing_mark_is_an_explanatory_error_not_a_traceback(self):
        # The mark was removed or hand-edited away (real at --write-docs time:
        # step 6 of the bump procedure runs right after editing nearby docs).
        # At main write_docs raises a bare ValueError out of main().
        self.plan.write_text("before\nno generated mark here\n\nafter\n")
        before = self.plan.read_bytes()
        with mock.patch.object(V, "METHOD_PLAN", self.plan), \
             mock.patch.object(U, "representability_table_md", return_value="NEW"):
            rc, out = self.run_main("--write-docs", "--json")
        self.assertEqual(rc, 1)
        j = json.loads(out)
        self.assertEqual(len(j["failures"]), 1)
        self.assertIn("method-plan.md", j["failures"][0])
        self.assertIn("generated-table anchor", j["failures"][0])
        self.assertEqual(self.plan.read_bytes(), before)  # the plan is untouched

    def test_missing_trailing_blank_line_is_an_explanatory_error(self):
        # The generated table is the last content, so the "\n\n" terminator
        # doesn't exist. Also a reported failure, not a ValueError traceback.
        self.plan.write_text("before\n<!-- generated from references/upstreams/"
                             "blis.json -->\nOLD TABLE\n")
        before = self.plan.read_bytes()
        with mock.patch.object(V, "METHOD_PLAN", self.plan), \
             mock.patch.object(U, "representability_table_md", return_value="NEW"):
            rc, out = self.run_main("--write-docs")
        self.assertEqual(rc, 1)
        self.assertIn("FAILURES  method-plan.md:", out)
        self.assertIn("generated-table anchor", out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(self.plan.read_bytes(), before)


@unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
class Main(unittest.TestCase):
    """main(): exit codes, --json shape, --write-docs, and that stale facts
    skip the machine checks entirely."""

    def setUp(self):
        self._cfg = tempfile.TemporaryDirectory()
        self.addCleanup(self._cfg.cleanup)
        os.environ["INVESTIGATION_CONFIG_HOME"] = self._cfg.name
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def run_main(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = V.main(list(argv))
        return rc, buf.getvalue()

    def test_green_machine_exits_zero_and_reports_lock_ids(self):
        rc, out = self.run_main("--json")
        self.assertEqual(rc, 0)
        j = json.loads(out)
        self.assertEqual(sorted(j), ["failures", "lock_ids", "notes", "ok", "stale",
                                     "warnings"])
        self.assertEqual(j["failures"], [])
        self.assertEqual(j["lock_ids"], {n: U.lock_id(n) for n in U.lock()})

    def test_text_output_prefixes_each_line_with_its_class(self):
        rc, out = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIn("WARNINGS ", out)
        self.assertNotIn("{", out)  # not JSON

    def test_failures_exit_one(self):
        with mock.patch.object(V, "check_engines",
                               lambda rep, machine=None: rep["failures"].append("x")):
            rc, _ = self.run_main()
        self.assertEqual(rc, 1)

    def test_stale_facts_fail_and_skip_the_machine_checks(self):
        calls = []
        def raise_stale(name):
            raise U.UpstreamError("stale: re-verify")
        patches = [mock.patch.object(U, "facts", raise_stale),
                   mock.patch.object(V, "check_engines", lambda r, m=None: calls.append(1)),
                   mock.patch.object(V, "check_blis", lambda r, m=None: calls.append(1)),
                   mock.patch.object(V, "check_notes", lambda r: calls.append(1))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        rc, out = self.run_main("--json")
        self.assertEqual(rc, 1)
        self.assertTrue(json.loads(out)["failures"])
        self.assertEqual(calls, [])  # machine checks never ran

    def test_write_docs_flag_reports_whether_it_wrote(self):
        plan = Path(self._tmp.name) / "plan.md"
        plan.write_text("x\n<!-- generated from references/upstreams/blis.json -->"
                        "\nOLD\n\ny\n")
        with mock.patch.object(V, "METHOD_PLAN", plan), \
             mock.patch.object(U, "representability_table_md", return_value="NEW"):
            rc, out = self.run_main("--write-docs", "--json")
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["docs_written"])

    def write_config(self, text: str) -> Path:
        p = Path(self._cfg.name) / "config.yaml"
        p.write_text(text)
        return p

    def test_malformed_machine_config_is_a_reported_failure_not_a_crash(self):
        # doctor.py's own convention: a broken user config is a reportable
        # condition, not a traceback. At main this raises ConfigError out of
        # check_engines and --json prints nothing at all.
        path = self.write_config("- just\n- a list\n")
        rc, out = self.run_main("--json")
        self.assertEqual(rc, 1)
        j = json.loads(out)
        self.assertEqual(len(j["failures"]), 1)
        self.assertIn("machine config", j["failures"][0])
        self.assertIn(str(path), j["failures"][0])
        self.assertIn("top level must be a mapping", j["failures"][0])

    def test_malformed_machine_config_text_mode_names_the_file(self):
        path = self.write_config("- just\n- a list\n")
        rc, out = self.run_main()
        self.assertEqual(rc, 1)
        self.assertIn("FAILURES  machine config:", out)
        self.assertIn(str(path), out)
        self.assertNotIn("Traceback", out)

    def test_unparseable_yaml_is_also_a_reported_failure(self):
        # Bad YAML syntax raises the same ConfigError from _config.load_yaml,
        # so the single except in main() covers both malformed shapes.
        self.write_config("foo: [unclosed\n")
        rc, out = self.run_main("--json")
        self.assertEqual(rc, 1)
        self.assertTrue(json.loads(out)["failures"])


if __name__ == "__main__":
    unittest.main()
