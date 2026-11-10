"""runtime.py submit / show / export, and: exported records grant nothing."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runtime_util import RUNTIME, SCRIPTS, envelope, request  # noqa: E402
from ains_runtime import store as S  # noqa: E402
from ains_runtime.export import export_study  # noqa: E402


def sh(*cmd, cwd=None):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = {k: v for k, v in os.environ.items()
                    if not (k.startswith("INVESTIGATION_") or k == "XDG_CONFIG_HOME")}
        self.env["INVESTIGATION_RUNS"] = str(self.tmp / "runs")
        self.env["INVESTIGATION_CONFIG_HOME"] = str(self.tmp / "cfg")
        self.repo = self.tmp / "demo-repo"
        self.repo.mkdir()
        sh("git", "init", "-q", "-b", "main", cwd=self.repo)
        self.study = self.repo / "investigations" / "s1"
        self.study.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args, stdin=None):
        return subprocess.run([sys.executable, str(RUNTIME), "--study", str(self.study),
                               *args], capture_output=True, text=True, env=self.env,
                              input=stdin, cwd=self.tmp)

    def write(self, msg, name="m.json"):
        p = self.tmp / name
        p.write_text(json.dumps(msg), encoding="utf-8")
        return p

    @property
    def db(self):
        return self.tmp / "runs" / "demo-repo" / "s1" / ".runtime" / S.DB_NAME


class Submit(CliCase):
    def test_submit_creates_db_under_runs_and_prints_receipt(self):
        r = self.run_cli("submit", str(self.write(request())))
        self.assertEqual(r.returncode, 0, r.stderr)
        receipt = json.loads(r.stdout)
        self.assertEqual(receipt["schema"], "ains.receipt/v1")
        self.assertEqual(receipt["body"]["status"], "accepted")
        self.assertTrue(self.db.is_file())
        # Nothing written into the study by submit.
        self.assertEqual(list(self.study.iterdir()), [])

    def test_duplicate_and_conflict_exit_codes(self):
        p = self.write(request())
        first = self.run_cli("submit", str(p))
        again = self.run_cli("submit", "-", stdin=p.read_text())
        self.assertEqual(again.returncode, 0)
        self.assertEqual(json.loads(again.stdout), json.loads(first.stdout))
        other = request(question="Different?")
        c = self.run_cli("submit", str(self.write(other, "o.json")))
        self.assertEqual(c.returncode, 1)
        self.assertEqual(json.loads(c.stdout)["body"]["status"], "conflict")

    def test_rejections(self):
        bad = request()
        bad["schema"] = "ains.experiment-request/v2"
        r = self.run_cli("submit", str(self.write(bad)))
        self.assertEqual(r.returncode, 1)
        self.assertIn("unsupported version", r.stdout)
        wrong_repo = request()
        wrong_repo["target"]["repo_name"] = "other-repo"
        r = self.run_cli("submit", str(self.write(wrong_repo)))
        self.assertEqual(r.returncode, 1)
        p = self.tmp / "dup.json"
        p.write_text('{"schema": "a", "schema": "b"}')
        self.assertEqual(self.run_cli("submit", str(p)).returncode, 2)

    def test_study_outside_git_needs_db(self):
        lone = self.tmp / "lone"
        lone.mkdir()
        r = subprocess.run([sys.executable, str(RUNTIME), "--study", str(lone), "submit",
                            str(self.write(request()))], capture_output=True, text=True,
                           env=self.env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--db", r.stderr)


class ShowExport(CliCase):
    def test_show_work_and_message(self):
        self.run_cli("submit", str(self.write(request())))
        r = self.run_cli("show", "w-1", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        view = json.loads(r.stdout)
        self.assertEqual(view["kind"], "work")
        self.assertEqual(view["messages"], ["req-1"])
        self.assertEqual(json.loads(self.run_cli("show", "req-1", "--json").stdout)["kind"],
                         "message")
        text = self.run_cli("show", "w-1")
        self.assertEqual(text.returncode, 0)
        self.assertIn("work w-1", text.stdout)
        self.assertEqual(self.run_cli("show", "nope").returncode, 1)

    def test_show_without_db_does_not_create_one(self):
        self.assertEqual(self.run_cli("show", "w-1").returncode, 1)
        self.assertFalse(self.db.exists())

    def test_export_writes_review_copies(self):
        self.run_cli("submit", str(self.write(request())))
        r = self.run_cli("export")
        self.assertEqual(r.returncode, 0, r.stderr)
        root = self.study / "runtime"
        rec = json.loads((root / "requests" / "req-1.json").read_text())
        self.assertEqual(rec["grants"], "nothing")
        self.assertEqual(rec["record"]["message"]["message_id"], "req-1")
        index = json.loads((root / "index.json").read_text())
        self.assertEqual(index["counts"]["requests"], 1)
        # Export is a rewrite: stale files disappear.
        (root / "requests" / "forged.json").write_text("{}")
        self.run_cli("export")
        self.assertFalse((root / "requests" / "forged.json").exists())

    def test_help_lists_only_r1_commands(self):
        r = subprocess.run([sys.executable, str(RUNTIME), "--help"], capture_output=True,
                           text=True)
        self.assertEqual(r.returncode, 0)
        for c in ("submit", "show", "export"):
            self.assertIn(c, r.stdout)


class CopiedStudyGrantsNothing(unittest.TestCase):
    """spec §6: copying a study without its runtime DB carries no permission.

    R3 extends this class with the real `launch` command (LaunchCopiedStudy
    below): a fully prepared and approved study, copied with its exported
    records and prepared inputs but WITHOUT its runtime DB, launches nothing.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = S.open_store(self.tmp / "a" / S.DB_NAME)
        self.assertTrue(self.src.submit(request(), env={}).ok)
        rec = {"capability": "nous", "material": {"q": "x", "commit": "c" * 40}}
        d = self.src.insert_preparation("prep-1", "w-1", rec, env={})
        self.src.insert_approval("appr-1", "prep-1", d, "chat", "go abc123", env={})
        self.study = self.tmp / "study"
        export_study(self.src, self.study, env={})
        self.copy = self.tmp / "copied-study"
        shutil.copytree(self.study, self.copy)
        self.fresh = S.open_store(self.tmp / "b" / S.DB_NAME)

    def tearDown(self):
        self.src.close()
        self.fresh.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_exported_preparation_cannot_launch_in_fresh_db(self):
        exported = json.loads((self.copy / "runtime" / "preparations" / "prep-1.json")
                              .read_text())
        self.assertEqual(exported["grants"], "nothing")
        self.assertTrue((self.copy / "runtime" / "approvals" / "appr-1.json").is_file())
        with self.assertRaisesRegex(S.GrantError, "no preparation"):
            self.fresh.reserve_invocation("inv-1", exported["record"]["prep_id"])
        self.assertEqual(self.fresh.list_invocations(), [])

    def test_exported_approval_cannot_be_attached_in_fresh_db(self):
        a = json.loads((self.copy / "runtime" / "approvals" / "appr-1.json").read_text())
        rec = a["record"]
        with self.assertRaises(S.GrantError):
            self.fresh.insert_approval(rec["approval_id"], rec["prep_id"],
                                       rec["material_digest"], rec["source"],
                                       rec["owner_quote"], env={})

    def test_replayed_request_plus_reprepared_needs_fresh_approval(self):
        # Even replaying the exported request and re-preparing the same
        # material in the fresh DB yields no grant without a NEW approval.
        req = json.loads((self.copy / "runtime" / "requests" / "req-1.json").read_text())
        self.assertTrue(self.fresh.submit(req["record"]["message"], env={}).ok)
        p = json.loads((self.copy / "runtime" / "preparations" / "prep-1.json").read_text())
        self.fresh.insert_preparation("prep-1", "w-1", p["record"]["preparation"], env={})
        with self.assertRaisesRegex(S.GrantError, "no approval"):
            self.fresh.reserve_invocation("inv-1", "prep-1")

    def test_runtime_package_has_no_import_path_for_exports(self):
        src = "\n".join(p.read_text() for p in (SCRIPTS / "ains_runtime").glob("*.py"))
        self.assertNotIn("def import_", src)
        self.assertNotIn("from_export", src)


class LaunchCopiedStudy(unittest.TestCase):
    """CopiedStudyGrantsNothing, through the real `runtime.py launch`."""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_runtime_prepare import git, go, make_repo
        import _runtime_fake_adapter as fake
        from ains_runtime import approval as AP, preparation as P
        self.P = P
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = make_repo(self.tmp)
        _, restore = fake.use()
        self.addCleanup(restore)
        self.env = {k: v for k, v in os.environ.items()
                    if not (k.startswith("INVESTIGATION_") or k == "XDG_CONFIG_HOME")}
        self.env.update(INVESTIGATION_RUNS=str(self.tmp / "runs"),
                        INVESTIGATION_CONFIG_HOME=str(self.tmp / "cfg"))
        self.src_db = self.tmp / "runs" / "demo-repo" / "s1" / ".runtime" / S.DB_NAME
        st = S.open_store(self.src_db)
        m = request(semantics={"models": {"session": "m"}, "run_id": "r1",
                               "experiment": "succeed"})
        m["target"]["commit"] = git(self.repo, "rev-parse", "HEAD")
        self.assertTrue(st.submit(m, env=self.env).ok)
        res = P.prepare(st, "w-1", target=self.repo, env=self.env)
        AP.approve(st, res.prep_id, code=res.short_code, source="chat",
                   quote=go(res.short_code), env=self.env)
        self.prep_id = res.prep_id
        study = self.repo / "investigations" / "s1"
        export_study(st, study, env=self.env)
        st.close()
        # Copy the study AND the .runtime dir minus the DB (prepared inputs,
        # card) to a second study location.
        self.copy = self.repo / "investigations" / "s2"
        shutil.copytree(study, self.copy)
        dst_rt = self.tmp / "runs" / "demo-repo" / "s2" / ".runtime"
        shutil.copytree(self.src_db.parent, dst_rt,
                        ignore=shutil.ignore_patterns(S.DB_NAME + "*"))
        self.addCleanup(P.make_writable, dst_rt)
        self.addCleanup(P.make_writable, self.src_db.parent)
        self.dst_db = dst_rt / S.DB_NAME

    def launch(self, *pre):
        return subprocess.run([sys.executable, str(Path(__file__).resolve().parent /
                                                   "_runtime_fake_cli.py"),
                               *pre, "launch", self.prep_id], capture_output=True,
                              text=True, env=self.env, timeout=60)

    def test_launch_in_copied_study_is_refused(self):
        self.assertFalse(self.dst_db.exists())
        r = self.launch("--study", str(self.copy))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no runtime DB", r.stderr)
        # With a fresh (empty) DB in place, still nothing: no preparation.
        S.open_store(self.dst_db).close()
        r = self.launch("--study", str(self.copy))
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("exported records grant nothing", r.stderr)
        with S.open_store(self.dst_db) as st:
            self.assertEqual(st.list_invocations(), [])
        self.assertFalse((self.tmp / "runs" / "demo-repo" / "s2" / "r1").exists())
        self.assertFalse((self.tmp / "runs" / "demo-repo" / "s1" / "r1").exists())
        # The original approval is still unused in the original DB.
        with S.open_store(self.src_db) as st:
            self.assertIsNone(st.get_approval(self.prep_id)["consumed_at"])


class Isolation(unittest.TestCase):
    def test_doctor_and_validator_do_not_import_runtime(self):
        for name in ("doctor.py", "validate_answer.py"):
            self.assertNotIn("ains_runtime", (SCRIPTS / name).read_text(), name)


if __name__ == "__main__":
    unittest.main()
