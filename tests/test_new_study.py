"""Tests for skills/investigation/scripts/new_study.py — scaffolding and the commit guard.

Fake $HOME / $INVESTIGATION_CONFIG_HOME and fake git repos in a tempdir; PATH
holds only `git`. new_study always calls doctor offline, so nothing touches
the network.
"""
import os
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills/investigation/scripts" / "new_study.py"

try:
    import yaml
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


def sh(*cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout


def make_repo(path: Path, remotes: dict) -> Path:
    path.mkdir(parents=True)
    sh("git", "init", "-q", "-b", "main", cwd=path)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
       "--allow-empty", "-m", "init", cwd=path)
    for name, url in remotes.items():
        sh("git", "remote", "add", name, url, cwd=path)
    return path


@unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
class NewStudyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.cfg = self.tmp / "cfg"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        os.symlink(shutil.which("git"), self.bin / "git")
        self.env = {"HOME": str(self.home), "PATH": str(self.bin),
                    "INVESTIGATION_CONFIG_HOME": str(self.cfg),
                    "INVESTIGATION_RUNS": str(self.tmp / "runs"),
                    "GIT_CONFIG_NOSYSTEM": "1", "LANG": "C.UTF-8",
                    **{k: v for k, v in os.environ.items()
                     if k.startswith("COVERAGE_")}}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_ns(self, cwd, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd, env=self.env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True)

    def run_ns_tty(self, cwd, *args, feed: bytes, timeout: float = 30.0):
        """Run new_study with stdin a real TTY and `feed` typed into it.

        ask_tracking() only runs when sys.stdin.isatty(); run_ns always pipes
        DEVNULL, so the interactive first-study prompt is unreachable there.
        A pty pair gives the child a TTY and us a master to write keystrokes
        to. Output is combined (the prompt is printed to stderr, the result
        to stdout) because both share the terminal in a real session too.
        """
        # Both stdout and stderr go to the pty, like a real terminal session:
        # the prompt (stderr) then has to appear on the master before we answer.
        master, slave = pty.openpty()
        try:
            p = subprocess.Popen([sys.executable, str(SCRIPT), *args], cwd=cwd,
                                 env=self.env, stdin=slave, stdout=slave,
                                 stderr=slave)
        finally:
            os.close(slave)
        chunks = []

        def drain(what: bytes | None):
            if what:
                chunks.append(what)
            return b"tracking [fork/untracked]" in b"".join(chunks)

        # Answer once the prompt reaches the terminal (the child is blocked on
        # readline; waiting for its exit would always burn the full timeout).
        # Reading past the child's exit raises EIO — the session is over.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and p.poll() is None:
            ready, _, _ = select.select([master], [], [], 0.05)
            if ready:
                try:
                    if drain(os.read(master, 4096)):
                        break
                except OSError:
                    break
        if p.poll() is None:
            os.write(master, feed)
        while time.monotonic() < deadline and p.poll() is None:
            ready, _, _ = select.select([master], [], [], 0.05)
            if ready:
                try:
                    drain(os.read(master, 4096))
                except OSError:
                    break
        if p.poll() is None:  # still blocked on the prompt: end the session
            try:
                os.close(master)
            except OSError:
                pass
            p.kill()
        p.wait(timeout=timeout)
        try:
            os.close(master)
        except OSError:
            pass
        out = b"".join(chunks).decode("utf-8", "replace")
        return p.returncode, out

    def fork_repo(self):
        return make_repo(self.tmp / "router", {"origin": "git@github.com:me/router.git",
                                               "upstream": "git@github.com:org/router.git"})

    def upstream_only_repo(self):
        return make_repo(self.tmp / "router", {"origin": "git@github.com:org/router.git"})


class TestScaffold(NewStudyBase):
    def test_fork_scaffold(self):
        repo = self.fork_repo()
        r = self.run_ns(repo, "--slug", "block-size", "--question", "Does 16 still hold?",
                        "--tracking", "fork")
        self.assertEqual(r.returncode, 0, r.stderr)
        study = repo / "investigations/block-size"
        for f in ("question.md", "plan.md", "hand-score.md"):
            self.assertTrue((study / f).is_file(), f)
        q = (study / "question.md").read_text()
        head = sh("git", "rev-parse", "HEAD", cwd=repo).strip()
        self.assertIn("Does 16 still hold?", q)
        self.assertIn(head, q)
        self.assertIn("git@github.com:org/router.git", q)  # the upstream URL is pinned
        self.assertIn("`main`", q)
        self.assertIn("Engine `nous`", q)
        self.assertIn("Engine `spotlights-engine`", q)
        self.assertIn(str(self.tmp / "runs" / "router" / "block-size"), q)
        self.assertNotIn("{{", q)
        plan = (study / "plan.md").read_text()
        self.assertIn("code-reading", plan)
        self.assertNotIn("{{", plan)
        self.assertNotIn("{{", (study / "hand-score.md").read_text())

        cfg = yaml.safe_load((repo / "investigations/config.yaml").read_text())
        self.assertEqual(cfg["tracking"], "fork")
        self.assertEqual(cfg["upstream_remote"], "upstream")
        self.assertFalse((self.cfg / "repos").exists())
        exclude = repo / ".git/info/exclude"
        self.assertNotIn("investigations/", exclude.read_text() if exclude.exists() else "")
        self.assertFalse(self.home.joinpath(".config").exists())

    def test_untracked_scaffold_and_exclude_idempotent(self):
        repo = self.fork_repo()
        r = self.run_ns(repo, "--slug", "s1", "--tracking", "untracked")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((repo / "investigations/config.yaml").exists())
        cfg = yaml.safe_load((self.cfg / "repos/router.yaml").read_text())
        self.assertEqual(cfg["tracking"], "untracked")
        lines = (repo / ".git/info/exclude").read_text().splitlines()
        self.assertEqual(lines.count("investigations/"), 1)
        self.assertEqual(sh("git", "status", "--porcelain", cwd=repo), "")

        # second study: mode comes from the config, exclude line not duplicated
        r = self.run_ns(repo, "--slug", "s2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("tracking: untracked", r.stdout)
        lines = (repo / ".git/info/exclude").read_text().splitlines()
        self.assertEqual(lines.count("investigations/"), 1)

    def test_first_study_without_tracking_and_no_tty_errors(self):
        repo = self.fork_repo()
        r = self.run_ns(repo, "--slug", "s1")
        self.assertEqual(r.returncode, 2)
        self.assertIn("--tracking", r.stderr)
        self.assertFalse((repo / "investigations").exists())

    def test_second_study_reuses_mode_and_rejects_conflict(self):
        repo = self.fork_repo()
        self.assertEqual(self.run_ns(repo, "--slug", "a", "--tracking", "fork").returncode, 0)
        r = self.run_ns(repo, "--slug", "b")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_ns(repo, "--slug", "c", "--tracking", "untracked")
        self.assertEqual(r.returncode, 2)
        self.assertIn("already says tracking: fork", r.stderr)

    def test_existing_slug_and_bad_slug(self):
        repo = self.fork_repo()
        self.assertEqual(self.run_ns(repo, "--slug", "a", "--tracking", "fork").returncode, 0)
        r = self.run_ns(repo, "--slug", "a")
        self.assertEqual(r.returncode, 2)
        self.assertIn("already exists", r.stderr)
        r = self.run_ns(repo, "--slug", "Bad Slug")
        self.assertEqual(r.returncode, 2)

    def test_upstream_push_remote_noted_at_scaffold(self):
        repo = self.upstream_only_repo()
        r = self.run_ns(repo, "--slug", "a", "--tracking", "fork")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("commit guard will refuse", r.stdout)

    def test_usage_error_when_no_slug_and_no_commit_guard(self):
        # main()'s argparse guard: neither --slug nor --commit-guard is a usage
        # error (exit 2, argparse's message on stderr), not a traceback.
        repo = self.fork_repo()
        r = self.run_ns(repo)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--slug is required", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_excludes_existing_content_without_trailing_newline(self):
        # add_exclude() normalizes a .git/info/exclude that lacks a trailing
        # newline: the previous last line must stay intact and whole, and
        # exactly one investigations/ line is appended.
        repo = self.fork_repo()
        exclude = repo / ".git/info/exclude"
        exclude.write_text("# no trailing newline comment")
        r = self.run_ns(repo, "--slug", "a", "--tracking", "untracked")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = exclude.read_text().splitlines()
        self.assertEqual(lines[0], "# no trailing newline comment")
        self.assertEqual(lines.count("investigations/"), 1)
        self.assertTrue(exclude.read_text().endswith("investigations/\n"))

    def test_second_untracked_study_readds_missing_exclude_and_notes_it(self):
        # The repo config already says untracked but the exclude line was
        # removed: ensure_repo_config re-adds it and says so in the notes.
        repo = self.fork_repo()
        self.assertEqual(self.run_ns(repo, "--slug", "a", "--tracking",
                                     "untracked").returncode, 0)
        exclude = repo / ".git/info/exclude"
        exclude.write_text("# removed the line\n")
        r = self.run_ns(repo, "--slug", "b")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"added investigations/ to {exclude}", r.stdout)
        self.assertEqual(exclude.read_text().splitlines().count("investigations/"), 1)

    def test_first_study_notes_undeterminable_upstream(self):
        # Two remotes, neither named upstream, no repo config yet: doctor
        # cannot tell which is upstream, so the freshly written config gets
        # upstream_remote: upstream as a guess plus a note telling the owner
        # to set it by hand before committing.
        repo = make_repo(self.tmp / "router", {
            "origin": "git@github.com:org/router.git",
            "other": "git@github.com:org/router-mirror.git"})
        r = self.run_ns(repo, "--slug", "a", "--tracking", "fork")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("upstream remote could not be determined", r.stdout)
        self.assertIn("set upstream_remote in", r.stdout)
        cfgp = repo / "investigations/config.yaml"
        self.assertEqual(yaml.safe_load(cfgp.read_text())["upstream_remote"], "upstream")

    def test_engine_rows_render_rich_versions_in_question_md(self):
        # A configured engine checkout (machine config engines.nous) gives
        # engine_rows() a found engine with version, sha, ref and pin, plus
        # the unpushed-HEAD warning: the markdown table a real question.md
        # ships with, not just "not found" rows.
        eng = make_repo(self.tmp / "eng", {"origin": "git@x:org/eng.git"})
        sh("git", "tag", "v1.2.3", cwd=eng)
        self.cfg.mkdir(parents=True, exist_ok=True)
        (self.cfg / "config.yaml").write_text(f"engines:\n  nous: {eng}\n")
        repo = self.fork_repo()
        r = self.run_ns(repo, "--slug", "a", "--tracking", "fork")
        self.assertEqual(r.returncode, 0, r.stderr)
        q = (repo / "investigations/a/question.md").read_text()
        row = next(line for line in q.splitlines() if "Engine `nous`" in line)
        self.assertIn("v1.2.3 @ ", row)
        self.assertIn("(main) via config", row)
        self.assertIn("; pin nous-v2-fixes@e224cc1", row)
        self.assertIn("WARN: ", row)
        self.assertIn("not found", next(line for line in q.splitlines()
                                        if "Engine `sim2real`" in line))


class TestInteractiveTracking(NewStudyBase):
    """The first-study prompt: only reachable when stdin is a TTY."""

    def test_prompt_asks_and_takes_untracked(self):
        repo = self.fork_repo()
        rc, out = self.run_ns_tty(repo, "--slug", "a", feed=b"untracked\n")
        self.assertEqual(rc, 0, out)
        self.assertIn("choose a tracking mode", out)
        self.assertIn("remotes: origin=", out)
        self.assertIn("upstream: upstream; push remote: origin", out)
        self.assertIn("tracking: untracked", out)
        # The choice is what lands on disk, not the fork default.
        self.assertFalse((repo / "investigations/config.yaml").exists())
        cfg = yaml.safe_load((self.cfg / "repos/router.yaml").read_text())
        self.assertEqual(cfg["tracking"], "untracked")
        self.assertEqual((repo / ".git/info/exclude").read_text()
                         .splitlines().count("investigations/"), 1)

    def test_empty_answer_defaults_to_fork(self):
        repo = self.fork_repo()
        rc, out = self.run_ns_tty(repo, "--slug", "a", feed=b"\n")
        self.assertEqual(rc, 0, out)
        self.assertIn("tracking: fork", out)
        cfg = yaml.safe_load((repo / "investigations/config.yaml").read_text())
        self.assertEqual(cfg["tracking"], "fork")
        # fork mode never touches .git/info/exclude
        self.assertFalse((repo / ".git/info/exclude").read_text()
                         .splitlines().count("investigations/"))

    def test_eof_at_the_prompt_is_a_clean_refusal(self):
        # Ctrl-D at the prompt: readline returns "" and ask_tracking raises
        # StudyError, which main() turns into exit 2 with a message — not a
        # traceback and not a silent default.
        repo = self.fork_repo()
        rc, out = self.run_ns_tty(repo, "--slug", "a", feed=b"\x04")
        self.assertEqual(rc, 2, out)
        self.assertIn("no tracking mode given", out)
        self.assertNotIn("Traceback", out)
        # Nothing was scaffolded and no config was written.
        self.assertFalse((repo / "investigations").exists())
        self.assertFalse((self.cfg / "repos/router.yaml").exists())


class TestCommitGuard(NewStudyBase):
    def test_refuses_when_push_remote_is_upstream(self):
        repo = self.upstream_only_repo()
        self.run_ns(repo, "--slug", "a", "--tracking", "fork")
        r = self.run_ns(repo, "--commit-guard")
        self.assertEqual(r.returncode, 1)
        self.assertIn("upstream", r.stderr)

    def test_refuses_when_branch_push_remote_is_named_upstream(self):
        repo = self.fork_repo()
        sh("git", "config", "branch.main.pushRemote", "upstream", cwd=repo)
        r = self.run_ns(repo, "--commit-guard")
        self.assertEqual(r.returncode, 1)
        self.assertIn("'upstream'", r.stderr)

    def test_allows_fork_push_remote(self):
        repo = self.fork_repo()
        self.run_ns(repo, "--slug", "a", "--tracking", "fork")
        r = self.run_ns(repo, "--commit-guard")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ok", r.stdout)

    def test_config_upstream_remote_overrides_heuristic(self):
        repo = self.fork_repo()
        self.run_ns(repo, "--slug", "a", "--tracking", "fork")
        cfgp = repo / "investigations/config.yaml"
        cfgp.write_text(cfgp.read_text().replace("upstream_remote: upstream",
                                                 "upstream_remote: origin"))
        r = self.run_ns(repo, "--commit-guard")
        self.assertEqual(r.returncode, 1)

    def test_refuses_in_untracked_mode(self):
        repo = self.fork_repo()
        self.run_ns(repo, "--slug", "a", "--tracking", "untracked")
        r = self.run_ns(repo, "--commit-guard")
        self.assertEqual(r.returncode, 1)
        self.assertIn("untracked", r.stderr)

    def test_refuses_outside_any_git_repo(self):
        # self.home is a plain directory inside the tempdir: no repo above it.
        r = self.run_ns(self.home, "--commit-guard")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not inside a git repo", r.stderr)

    def test_refuses_on_detached_head(self):
        repo = self.fork_repo()
        self.run_ns(repo, "--slug", "a", "--tracking", "fork")
        head = sh("git", "rev-parse", "HEAD", cwd=repo).strip()
        sh("git", "checkout", "-q", "--detach", head, cwd=repo)
        r = self.run_ns(repo, "--commit-guard")
        self.assertEqual(r.returncode, 1)
        self.assertIn("detached HEAD", r.stderr)

    def test_refuses_when_upstream_remote_is_ambiguous(self):
        # origin plus a second remote, neither named 'upstream', and no repo
        # config yet: doctor can't tell which is upstream, so the guard refuses
        # rather than guess. (Scaffolding first would resolve it: the written
        # config pins upstream_remote, so this refusal is the no-config path.)
        repo = make_repo(self.tmp / "router", {
            "origin": "git@github.com:org/router.git",
            "other": "git@github.com:org/router-mirror.git"})
        r = self.run_ns(repo, "--commit-guard")
        self.assertEqual(r.returncode, 1)
        self.assertIn("upstream remote unknown", r.stderr)
        self.assertIn("upstream_remote in the repo config", r.stderr)

    def test_refuses_when_branch_has_no_push_remote(self):
        # 'upstream' resolves (a remote by that name exists) but the branch has
        # no configured remote and no origin to fall back to: nowhere to push.
        repo = make_repo(self.tmp / "router", {
            "upstream": "git@github.com:org/router.git",
            "mirror": "git@github.com:org/router-mirror.git"})
        self.run_ns(repo, "--slug", "a", "--tracking", "fork")
        r = self.run_ns(repo, "--commit-guard")
        self.assertEqual(r.returncode, 1)
        self.assertIn("has no push remote", r.stderr)


class TestScaffoldRefusals(NewStudyBase):
    def test_refuses_outside_any_git_repo(self):
        r = self.run_ns(self.home, "--slug", "a", "--tracking", "fork")
        self.assertEqual(r.returncode, 2)
        self.assertIn("not inside a git repo", r.stderr)

    def test_refuses_when_repo_config_tracking_is_invalid(self):
        repo = self.fork_repo()
        self.run_ns(repo, "--slug", "a", "--tracking", "fork")
        cfgp = repo / "investigations/config.yaml"
        cfgp.write_text("tracking: sometimes\n")
        r = self.run_ns(repo, "--slug", "b", "--tracking", "fork")
        self.assertEqual(r.returncode, 2)
        self.assertIn("`tracking` must be fork or untracked", r.stderr)
        self.assertIn("sometimes", r.stderr)
        self.assertFalse((repo / "investigations/b").exists())


if __name__ == "__main__":
    unittest.main()
