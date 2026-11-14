"""Tests for skills/investigation/scripts/new_study.py — scaffolding and the commit guard.

Fake $HOME / $INVESTIGATION_CONFIG_HOME and fake git repos in a tempdir; PATH
holds only `git`. new_study always calls doctor offline, so nothing touches
the network.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cov_child_env import covsite_env  # noqa: E402

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
                     if k.startswith("COVERAGE_")},
                    **covsite_env()}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_ns(self, cwd, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd, env=self.env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True)

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
