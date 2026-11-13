"""runtime.py command handlers, called in-process (plan §3.4).

The CLI is normally exercised as a subprocess (test_runtime_cli.py,
test_runtime_prepare.py, test_runtime_exec.py), and those children write
their coverage data into the test's temp dir, which tearDown deletes — so
cli.py's handler bodies read as uncovered even though the paths run. This
module calls ains_runtime.cli.main() directly, with the test-only fake
adapter registered, so every handler, its error branch and its exit code
are measured in this process.

What is covered here and nowhere else:
  - the UsageError branches of Context.db_path() (no --study/--db at all;
    a --study outside any git repo)
  - each command's own error path: prepare/approve/launch/status/cancel/
    reconcile/collect/result print the refusal and exit 1
  - recover's happy path (reconcile every in-flight invocation)
  - result's usage guard (--explanation without --outcome) and its
    not-exported note (no --study/--out)
  - show of an invocation (the fourth lookup kind)
  - _hint for a --db-only context
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _runtime_fake_adapter as fake  # noqa: E402
from _runtime_util import kill_matching, request, wait_for  # noqa: E402
from ains_runtime import approval as AP  # noqa: E402
from ains_runtime import preparation as P  # noqa: E402
from ains_runtime import store as S  # noqa: E402
from ains_runtime import supervisor as SV  # noqa: E402
from ains_runtime.cli import main  # noqa: E402
from test_runtime_prepare import git, go, make_repo  # noqa: E402

SECRET = "sk-planted-cli-handlers-9f8e7d6c5b4a"


def cli_main(argv):
    """Run the CLI in-process; returns (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = main(argv)
    return rc, out.getvalue(), err.getvalue()


class Base(unittest.TestCase):
    """A git repo with a study, a runtime DB, and the fake adapter ready.

    The environment variables the CLI reads (INVESTIGATION_RUNS,
    INVESTIGATION_CONFIG_HOME) are pointed at the temp dir both in this
    process (db_path() consults _config, which reads the live environ) and
    in any child the handlers spawn.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        self.repo = make_repo(self.tmp)
        self.commit = git(self.repo, "rev-parse", "HEAD")
        self.study = self.repo / "investigations" / "s1"
        self.study.mkdir(parents=True)
        self.runs = self.tmp / "runs"
        self.adapter, restore = fake.use()
        self.addCleanup(restore)
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "HOME": str(self.tmp), "INVESTIGATION_RUNS": str(self.runs),
                    "INVESTIGATION_CONFIG_HOME": str(self.tmp / "cfg"),
                    "CLAUDE_CODE_ENTRYPOINT": "cli", "FAKE_API_TOKEN": SECRET}
        self._saved_env = {k: os.environ[k] for k in
                           ("INVESTIGATION_RUNS", "INVESTIGATION_CONFIG_HOME")
                           if k in os.environ}
        os.environ.update(self.env)
        self.db = self.runs / "demo-repo" / "s1" / ".runtime" / S.DB_NAME
        self.store = S.open_store(self.db)
        self.n = 0

    def _cleanup(self):
        kill_matching(self.tmp.name)
        SV.reap(timeout=5)
        self.store.close()
        P.make_writable(P.prepared_root(self.store))
        for k in ("INVESTIGATION_RUNS", "INVESTIGATION_CONFIG_HOME"):
            os.environ.pop(k, None)
        os.environ.update(self._saved_env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- state builders (same route the subprocess tests use) ----------------

    def msg(self, **over):
        self.n += 1
        m = request(f"req-{self.n}", work_id=f"w-{self.n}",
                    semantics={"models": {"session": "model-x"},
                               "run_id": f"fakerun{self.n:04d}",
                               "experiment": "succeed",
                               "secrets": ["FAKE_API_TOKEN"]}, **over)
        m["target"]["commit"] = self.commit
        return m

    def submitted(self):
        """submit via the CLI handler; returns the work id."""
        m = self.msg()
        f = self.tmp / f"m{self.n}.json"
        f.write_text(json.dumps(m))
        rc, out, err = self.cli("submit", str(f))
        self.assertEqual(rc, 0, err)
        return m["work_id"]

    def prepared(self):
        """submit + prepare via the CLI handlers; returns the prep id."""
        wid = self.submitted()
        rc, out, err = self.cli("prepare", wid, "--json")
        self.assertEqual(rc, 0, err)
        return json.loads(out)["prep_id"]

    def approved(self):
        """submit + prepare + approve via the CLI handlers; returns the prep id."""
        prep = self.prepared()
        rc, out, err = self.cli("approve", prep, "--code",
                                self.code_for(prep), "--source", "chat",
                                "--quote", go(self.code_for(prep)))
        self.assertEqual(rc, 0, err)
        return prep

    def code_for(self, prep):
        rec = self.store.get_preparation(prep)["record"]
        from ains_runtime.canonical import short_code
        return short_code(rec["material_digest"])

    # -- the CLI itself -------------------------------------------------------

    def cli(self, *args):
        """main() with this study; env is already in os.environ."""
        return cli_main(["--study", str(self.study), *args])


class Usage(Base):
    def test_no_study_and_no_db_is_a_usage_error(self):
        rc, out, err = cli_main(["status", "inv-nope"])
        self.assertEqual(rc, 2)
        self.assertIn("give --study DIR (or --db PATH)", err)

    def test_study_outside_any_git_repo_is_a_usage_error(self):
        lone = self.tmp / "lone"
        lone.mkdir()
        rc, out, err = cli_main(["--study", str(lone), "status", "inv-nope"])
        self.assertEqual(rc, 2)
        self.assertIn("not inside a git repo; use --db", err)

    def test_db_only_context_hint_names_the_db(self):
        # The card's approve command names the context it was prepared in:
        # with --db (no --study) the hint is the --db form, not --study.
        wid = self.submitted()
        rc, out, err = cli_main(["--db", str(self.db), "prepare", wid,
                                 "--target", str(self.repo)])
        self.assertEqual(rc, 0, err)
        self.assertIn(f"runtime.py --db {self.db} approve", out)
        self.assertNotIn(f"runtime.py --study {self.study} approve", out)

    def test_prepare_without_a_target_or_study_is_a_usage_error(self):
        rc, out, err = cli_main(["--db", str(self.db), "prepare", "w-1"])
        self.assertEqual(rc, 2)
        self.assertIn("prepare needs --target REPO (or --study DIR)", err)


class SubmitShow(Base):
    def test_submit_with_a_wrong_repo_name_is_refused_not_crashed(self):
        m = self.msg()
        m["target"]["repo_name"] = "other-repo"
        f = self.tmp / "wrong-repo.json"
        f.write_text(json.dumps(m))
        rc, out, err = self.cli("submit", str(f))
        self.assertEqual(rc, 1)
        v = json.loads(out)
        self.assertEqual(v["status"], "rejected")
        self.assertIn("'other-repo' is not this study's repo 'demo-repo'",
                      v["reasons"][0])
        # Nothing was recorded for a mismatched target.
        self.assertIsNone(self.store.get_work("w-1"))

    def test_submit_of_an_invalid_message_prints_each_reason(self):
        bad = self.msg()
        bad["schema"] = "ains.experiment-request/v2"
        f = self.tmp / "bad-schema.json"
        f.write_text(json.dumps(bad))
        rc, out, err = self.cli("submit", str(f))
        self.assertEqual(rc, 1)
        self.assertIn("submit:", err)          # the reasons go to stderr
        self.assertIsNone(self.store.get_work("w-1"))

    def test_submit_with_db_only_has_no_repo_name_check(self):
        # --db only: repo_name() is None, so the target.repo_name guard is
        # skipped and the message is validated on its own merits.
        m = self.msg()
        m["target"]["repo_name"] = "somewhere-else"
        f = self.tmp / "db-only.json"
        f.write_text(json.dumps(m))
        rc, out, err = cli_main(["--db", str(self.db), "submit", str(f)])
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["body"]["status"], "accepted")

    def test_a_store_error_reaches_main_and_exits_2(self):
        # A file that is not a runtime DB: opening it raises StoreError, which
        # main() turns into exit 2 (not a traceback). The message file must be
        # loadable first, so the failure is the store, not the input.
        junk = self.tmp / "junk.sqlite"
        junk.write_bytes(b"this is not a database")
        good = self.tmp / "good.json"
        good.write_text(json.dumps(self.msg()))
        rc, out, err = cli_main(["--db", str(junk), "submit", str(good)])
        self.assertEqual(rc, 2)
        self.assertIn("runtime.py:", err)

    def test_show_prints_each_lookup_kind_and_refuses_the_unknown(self):
        wid = self.submitted()
        prep = self.prepared()
        rc, out, err = self.cli("show", prep, "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["kind"], "preparation")
        rc, out, err = self.cli("show", "w-1", "--json")
        self.assertEqual(json.loads(out)["kind"], "work")
        rc, out, err = self.cli("show", "req-1", "--json")
        self.assertEqual(json.loads(out)["kind"], "message")
        rc, out, err = self.cli("show", "nope")
        self.assertEqual(rc, 1)
        self.assertIn("no work, preparation, invocation or message", err)

    def test_show_text_rendering_of_an_invocation(self):
        # The fourth lookup kind, plus the text (non---json) rendering with
        # its events list.
        prep = self.approved()
        inv_id, _, _ = SV.reserve(self.store, prep, env=self.env, inv_id="inv-show")
        rc, out, err = self.cli("show", "inv-show")
        self.assertEqual(rc, 0, err)
        self.assertIn("invocation inv-show", out)
        self.assertIn("state: reserved", out)
        self.assertIn("state:reserved", out)      # events are listed

    def test_show_without_a_db_refuses_and_creates_nothing(self):
        fresh = self.tmp / "runs2"
        os.environ["INVESTIGATION_RUNS"] = str(fresh)
        try:
            rc, out, err = self.cli("show", "w-1")
        finally:
            os.environ["INVESTIGATION_RUNS"] = str(self.runs)
        self.assertEqual(rc, 1)
        self.assertIn("no runtime DB", err)
        self.assertFalse(fresh.exists())


class PrepareApprove(Base):
    def test_prepare_refusal_lists_reasons_and_exits_1(self):
        rc, out, err = self.cli("prepare", "w-nope")
        self.assertEqual(rc, 1)
        self.assertIn("prepare: no work", err)
        self.assertEqual(self.store.list_preparations(), [])

    def test_prepare_prints_the_card_by_default(self):
        wid = self.submitted()
        rc, out, err = self.cli("prepare", wid)
        self.assertEqual(rc, 0, err)
        prep = self.store.list_preparations()[0]["prep_id"]
        card = (P.prep_dir(self.store, prep) / "card.md").read_text()
        self.assertEqual(out, card)

    def test_approve_refusal_exits_1(self):
        prep = self.prepared()
        # A wrong code is refused and leaves no approval behind.
        rc, out, err = self.cli("approve", prep, "--code", "0" * 12,
                                "--source", "chat", "--quote", go("0" * 12))
        self.assertEqual(rc, 1)
        self.assertIn("approve:", err)
        self.assertIsNone(self.store.get_approval(prep))
        # A used approval stays used: the second go is refused.
        code = self.code_for(prep)
        self.assertEqual(self.cli("approve", prep, "--code", code, "--source", "chat",
                                  "--quote", go(code))[0], 0)
        rc, out, err = self.cli("approve", prep, "--code", code, "--source", "chat",
                                "--quote", go(code))
        self.assertEqual(rc, 1)
        self.assertIn("already approved", err)
        # The recorded approval is untouched by the refused second go.
        self.assertIsNotNone(self.store.get_approval(prep))

    def test_approve_success_prints_the_receipt(self):
        prep = self.prepared()
        code = self.code_for(prep)
        rc, out, err = self.cli("approve", prep, "--code", code, "--source", "chat",
                                "--quote", go(code))
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["short_code"], code)


class RecoveryCommands(Base):
    def test_status_of_an_unknown_id_exits_1(self):
        rc, out, err = self.cli("status", "nope")
        self.assertEqual(rc, 1)
        self.assertIn("status: no work or invocation", err)

    def test_status_of_a_work_id_lists_its_invocations(self):
        prep = self.approved()
        SV.reserve(self.store, prep, env=self.env, inv_id="inv-st1")
        rc, out, err = self.cli("status", "w-1")
        self.assertEqual(rc, 0, err)
        v = json.loads(out)
        self.assertEqual(v["work_id"], "w-1")
        self.assertEqual(v["invocations"][0]["inv_id"], "inv-st1")

    def test_cancel_of_an_unknown_invocation_exits_1(self):
        rc, out, err = self.cli("cancel", "inv-nope", "--reason", "owner asked")
        self.assertEqual(rc, 1)
        self.assertIn("cancel: no invocation", err)

    def test_cancel_of_a_terminal_invocation_reports_it_is_not_applicable(self):
        # Already terminal: the cancel is recorded as not applicable, the real
        # state is returned unchanged, and the handler exits 0.
        prep = self.approved()
        SV.reserve(self.store, prep, env=self.env, inv_id="inv-done")
        self.store.transition("inv-done", (SV.RESERVED,), SV.CANCELLED,
                              ended_at=S.now(), detail={"reason": "test"})
        rc, out, err = self.cli("cancel", "inv-done", "--reason", "owner asked")
        self.assertEqual(rc, 0, err)
        v = json.loads(out)
        self.assertEqual(v["state"], SV.CANCELLED)
        self.assertFalse(v["cancel"]["requested"])
        self.assertIn("already cancelled", v["cancel"]["note"])
        events = [e["event"] for e in self.store.list_events(entity_id="inv-done")]
        self.assertIn("cancel:not_applicable", events)

    def test_recover_reconciles_nothing_and_reports_it(self):
        rc, out, err = self.cli("recover")
        self.assertEqual(rc, 0, err)
        v = json.loads(out)
        self.assertEqual(v["reconciled"], [])
        self.assertEqual(v["recovery_required"], [])

    def test_reconcile_refusals_exit_1(self):
        # Without a quote.
        rc, out, err = self.cli("reconcile", "inv-nope", "--mark-not-launched")
        self.assertEqual(rc, 1)
        self.assertIn("reconcile: --quote is required", err)
        # A quote that is really a secret value is refused.
        rc, out, err = self.cli("reconcile", "inv-nope", "--mark-not-launched",
                                "--quote", f"go ahead {SECRET}")
        self.assertEqual(rc, 1)
        self.assertIn("secret", err)
        # Not recovery_required (here: unknown) is refused.
        rc, out, err = self.cli("reconcile", "inv-nope", "--mark-not-launched",
                                "--quote", "never launched, sorry")
        self.assertEqual(rc, 1)
        self.assertIn("no invocation", err)

    def test_reconcile_marks_a_recovery_required_invocation_not_launched(self):
        prep = self.approved()
        inv_id, _, _ = SV.reserve(self.store, prep, env=self.env,
                                  inv_id="inv-recov")
        # The launcher is gone and nothing was spawned: recovery_required.
        self.store.transition(inv_id, (SV.RESERVED,), SV.RECOVERY_REQUIRED,
                              detail={"reason": "test"})
        rc, out, err = self.cli("reconcile", "inv-recov", "--mark-not-launched",
                                "--quote", "owner: it never launched")
        self.assertEqual(rc, 0, err)
        v = json.loads(out)
        self.assertEqual(v["state"], SV.NOT_LAUNCHED)
        self.assertEqual(v["lifecycle"], "rejected")


class ResultCommands(Base):
    def _terminal_invocation(self, inv_id):
        """Force a reserved invocation to a terminal state without spawning."""
        self.store.transition(inv_id, (SV.RESERVED,), SV.CANCELLED, ended_at=S.now(),
                              detail={"reason": "test: nothing ran"})

    def test_collect_of_an_unknown_invocation_exits_1(self):
        rc, out, err = self.cli("collect", "inv-nope")
        self.assertEqual(rc, 1)
        self.assertIn("collect: no invocation", err)

    def test_collect_of_a_non_terminal_invocation_reports_not_run(self):
        # Reserved with this process as the live launcher: collect answers
        # "not_run" with the run-`status` note (exit 0, nothing collected).
        prep = self.approved()
        SV.reserve(self.store, prep, env=self.env, inv_id="inv-live")
        rc, out, err = self.cli("collect", "inv-live")
        self.assertEqual(rc, 0, err)
        v = json.loads(out)
        self.assertEqual(v["collection"]["state"], "not_run")
        self.assertIn("run `status` until it is terminal",
                      v["collection"]["reason"])

    def test_result_explanation_without_outcome_is_a_usage_error(self):
        rc, out, err = self.cli("result", "inv-nope", "--explanation", "x")
        self.assertEqual(rc, 2)
        self.assertIn("--explanation needs --outcome", err)

    def test_result_of_an_unknown_invocation_exits_1(self):
        rc, out, err = self.cli("result", "inv-nope")
        self.assertEqual(rc, 1)
        self.assertIn("result: no invocation", err)

    def test_result_without_a_study_notes_it_is_not_exported(self):
        prep = self.approved()
        inv_id, _, _ = SV.reserve(self.store, prep, env=self.env, inv_id="inv-res")
        self._terminal_invocation("inv-res")
        # --db only: no study to export into, so the note says so.
        rc, out, err = cli_main(["--db", str(self.db), "result", "inv-res"])
        self.assertEqual(rc, 0, err)
        v = json.loads(out)
        self.assertIsNone(v["exported_to"])
        self.assertTrue(any("give --study" in n for n in v["notes"]), v["notes"])

    def test_result_with_a_study_exports_the_record(self):
        prep = self.approved()
        SV.reserve(self.store, prep, env=self.env, inv_id="inv-res2")
        self._terminal_invocation("inv-res2")
        rc, out, err = self.cli("result", "inv-res2")
        self.assertEqual(rc, 0, err)
        v = json.loads(out)
        self.assertEqual(v["exported_to"],
                         str(self.study / "runtime" / "results" / "inv-res2.json"))
        rec = json.loads(Path(v["exported_to"]).read_text())
        self.assertEqual(rec["grants"], "nothing")


class ExportCommand(Base):
    def test_export_needs_a_study(self):
        rc, out, err = cli_main(["--db", str(self.db), "export"])
        self.assertEqual(rc, 2)
        self.assertIn("export needs --study DIR", err)

    def test_export_without_a_db_refuses_and_exits_1(self):
        fresh = self.tmp / "runs3"
        os.environ["INVESTIGATION_RUNS"] = str(fresh)
        try:
            rc, out, err = self.cli("export")
        finally:
            os.environ["INVESTIGATION_RUNS"] = str(self.runs)
        self.assertEqual(rc, 1)
        self.assertIn("export:", err)
        self.assertFalse((self.study / "runtime").exists())


class LaunchCommand(Base):
    """cmd_launch's success path: the warning loop, the receipt and exit 0.

    A real (fake) launch: the handler spawns the detached supervisor, which
    runs the `succeed` engine and exits. Cleanup kills anything still alive
    by temp-dir name, exactly as test_runtime_exec does.
    """

    def test_launch_prints_the_invocation_and_a_drift_warning(self):
        prep = self.approved()
        # An observed (non-material) env var appearing between prepare and
        # launch is a warning on stderr, never a refusal.
        os.environ["CLAUDE_NEW"] = "x"
        try:
            rc, out, err = self.cli("launch", prep)
        finally:
            os.environ.pop("CLAUDE_NEW", None)
        self.assertEqual(rc, 0, err)
        v = json.loads(out)
        self.assertEqual(v["state"], SV.RESERVED)
        self.assertTrue(v["inv_id"].startswith("inv-"))
        self.assertTrue(any("launch: warning: $CLAUDE_NEW" in line
                            for line in err.splitlines()), err)
        # The supervisor records the engine's exit; classification (a terminal
        # state) is collect/status's job, so `exited` with an exit code is done.
        wait_for(lambda: self.store.get_invocation(v["inv_id"])["exit_code"] is not None,
                 what="the engine to finish")
        # The secret never reaches the CLI's own streams.
        self.assertNotIn(SECRET, out + err)

    def test_launch_of_an_unapproved_preparation_exits_1(self):
        prep = self.prepared()
        rc, out, err = self.cli("launch", prep)
        self.assertEqual(rc, 1)
        self.assertIn("launch: preparation", err)
        self.assertIn("has no approval", err)
        self.assertEqual(self.store.list_invocations(), [])


class SuperviseCommand(Base):
    """The private `_supervise` command's dispatch: db_path() to exit code.

    Normally only the detached supervisor child (spawned by launch) runs
    this command, so its handler lines never execute in a measured process.
    Called in-process here on an invocation reserved for the `succeed`
    engine, it runs the engine to exit and returns the supervisor's code.
    """

    def test_supervise_runs_the_engine_and_exits_0(self):
        prep = self.approved()
        inv_id, _, _ = SV.reserve(self.store, prep, env=self.env,
                                  inv_id="inv-sup")
        # spawn_supervisor normally creates the logs dir; this in-process
        # supervisor run must not depend on that side effect.
        SV.logs_dir(self.store).mkdir(parents=True, exist_ok=True)
        rc, out, err = cli_main(["--db", str(self.db), "_supervise", "inv-sup"])
        self.assertEqual(rc, 0, err)
        inv = self.store.get_invocation("inv-sup")
        self.assertEqual(inv["state"], SV.EXITED)
        self.assertEqual(inv["exit_code"], 0)
        # The supervisor's log lines went to this process's stdout.
        self.assertIn("engine pid", out)
        self.assertIn("engine exited 0", out)

    def test_supervise_of_an_unknown_invocation_exits_2(self):
        rc, out, err = cli_main(["--db", str(self.db), "_supervise", "inv-nope"])
        self.assertEqual(rc, 2)
        self.assertIn("no reserved invocation 'inv-nope'", out)


class SecretHygiene(Base):
    def test_no_command_prints_a_secret_value(self):
        prep = self.approved()
        SV.reserve(self.store, prep, env=self.env, inv_id="inv-sec")
        outs = []
        for args in (["submit", "/dev/null"], ["show", "w-1"], ["status", "w-1"],
                     ["collect", "inv-sec"], ["result", "inv-sec"],
                     ["recover"], ["export"]):
            rc, out, err = self.cli(*args)
            outs.append(out + err)
        self.assertNotIn(SECRET, "".join(outs))


if __name__ == "__main__":
    unittest.main()
