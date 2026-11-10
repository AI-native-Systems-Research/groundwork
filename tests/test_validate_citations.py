"""Tests for validate_answer.py --check-citations (P5, finding F4).

Every test builds a throwaway git repo in a tempdir (the "cited" repo) and a
study dir with final/answer.json + citations/<id>.txt, then runs the real CLI.
The machine config is isolated with INVESTIGATION_CONFIG_HOME -> a tempdir so
the owner's ~/.config/investigation never leaks in.

Run from the repo root:  python3 -m unittest tests.test_validate_citations -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills/investigation/scripts" / "validate_answer.py"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}

SOURCE = (
    "package kvblock\n"
    "\n"
    "// TokensToKVBlockKeys hashes token chunks.\n"
    "func (db *ChunkedTokenDatabase) TokensToKVBlockKeys(tokens []uint32) []Key {\n"
    "\treturn db.hash(tokens)\n"
    "}\n"
)
VALUE = "func (db *ChunkedTokenDatabase) TokensToKVBlockKeys"


def git(cwd, *args):
    env = {**os.environ, **GIT_ENV}
    r = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", *args],
        cwd=cwd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {args} failed: {r.stderr}")
    return r.stdout.strip()


def make_repo(path: Path, origin=None) -> str:
    """Create a git repo with one commit holding SOURCE; returns its sha."""
    path.mkdir(parents=True)
    git(path, "init", "-q")
    f = path / "pkg/kvcache/kvblock/token_processor.go"
    f.parent.mkdir(parents=True)
    f.write_text(SOURCE)
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "init")
    if origin:
        git(path, "remote", "add", "origin", origin)
    return git(path, "rev-parse", "HEAD")


class CitationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.cfg_home = self.tmp / "cfg"
        self.cfg_home.mkdir()
        self.cited = self.tmp / "checkout"
        self.sha = make_repo(self.cited)
        # The study lives outside any git repo unless a test says otherwise.
        self.study = self.tmp / "study"
        (self.study / "final").mkdir(parents=True)
        (self.study / "citations").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    # -- helpers ------------------------------------------------------------
    def write_answer(self, study=None, evidence="citations/c1.txt", value=VALUE):
        study = study or self.study
        data = {
            "answer": "The prefix scorer hashes blocks in TokensToKVBlockKeys.",
            "deliverable": None,
            "repro": "true",
            "claims": [{"id": "c1", "metric": "block hashing site", "value": value,
                        "condition": "llm-d-router @ sha, token_processor.go:L4",
                        "evidence": evidence}],
        }
        p = study / "final" / "answer.json"
        p.write_text(json.dumps(data))
        return p

    def write_citation(self, header, body, study=None, name="c1.txt"):
        study = study or self.study
        p = study / "citations" / name
        p.write_text((header + "\n" if header is not None else "") + body)
        return p

    def run_cli(self, answer, *extra):
        env = {**os.environ, **GIT_ENV, "INVESTIGATION_CONFIG_HOME": str(self.cfg_home)}
        env.pop("XDG_CONFIG_HOME", None)
        return subprocess.run([sys.executable, str(SCRIPT), str(answer), *extra],
                              capture_output=True, text=True, cwd=self.tmp, env=env)

    def header(self, start=3, end=4, repo="llm-d-router", sha=None):
        return (f"{repo}@{sha or self.sha}:pkg/kvcache/kvblock/token_processor.go"
                f":L{start}-L{end}")

    def quoted(self, start=3, end=4):
        return "".join(SOURCE.splitlines(keepends=True)[start - 1:end])

    # -- cases --------------------------------------------------------------
    def test_valid_citation_with_repo_flag(self):
        self.write_citation(self.header(), self.quoted())
        ans = self.write_answer()
        r = self.run_cli(ans, "--check-citations", "--repo", f"llm-d-router={self.cited}")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("c1 citation OK", r.stdout)
        self.assertIn("OK: 1 citations checked", r.stdout)

    def test_trailing_newline_tolerated(self):
        self.write_citation(self.header(), self.quoted().rstrip("\n"))
        r = self.run_cli(self.write_answer(), "--check-citations",
                         "--repo", f"llm-d-router={self.cited}")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_without_flag_citations_not_checked(self):
        # A bogus header passes the plain validator: the value is in the file.
        self.write_citation("not a header", VALUE + "\n")
        r = self.run_cli(self.write_answer())
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("citation OK", r.stdout)
        self.assertNotIn("citations checked", r.stdout)

    def test_wrong_lines(self):
        # Quote lines 3-4 but claim they sit at 2-3.
        self.write_citation(self.header(start=2, end=3), self.quoted(3, 4))
        r = self.run_cli(self.write_answer(), "--check-citations",
                         "--repo", f"llm-d-router={self.cited}")
        self.assertEqual(r.returncode, 1)
        self.assertIn("c1: citation mismatch at line 2", r.stderr)

    def test_mismatch_reports_first_differing_line(self):
        body = SOURCE.splitlines(keepends=True)[2] + "func changed()\n"
        self.write_citation(self.header(3, 4), body)
        r = self.run_cli(self.write_answer(), "--check-citations",
                         "--repo", f"llm-d-router={self.cited}")
        self.assertEqual(r.returncode, 1)
        self.assertIn("citation mismatch at line 4", r.stderr)

    def test_whitespace_difference_is_a_mismatch(self):
        self.write_citation(self.header(5, 5), "    return db.hash(tokens)\n")
        r = self.run_cli(self.write_answer(value="db.hash"), "--check-citations",
                         "--repo", f"llm-d-router={self.cited}")
        self.assertEqual(r.returncode, 1)
        self.assertIn("citation mismatch at line 5", r.stderr)

    def test_out_of_range_lines(self):
        self.write_citation(self.header(5, 99), self.quoted(5, 6))
        r = self.run_cli(self.write_answer(value="db.hash"), "--check-citations",
                         "--repo", f"llm-d-router={self.cited}")
        self.assertEqual(r.returncode, 1)
        self.assertIn("out of range", r.stderr)

    def test_stale_sha(self):
        self.write_citation(self.header(sha="deadbeef" * 5), self.quoted())
        r = self.run_cli(self.write_answer(), "--check-citations",
                         "--repo", f"llm-d-router={self.cited}")
        self.assertEqual(r.returncode, 1)
        self.assertIn("c1: citation sha " + "deadbeef" * 5 + " not found", r.stderr)

    def test_path_not_at_sha(self):
        hdr = f"llm-d-router@{self.sha}:pkg/gone.go:L1-L1"
        self.write_citation(hdr, VALUE + "\n")
        r = self.run_cli(self.write_answer(), "--check-citations",
                         "--repo", f"llm-d-router={self.cited}")
        self.assertEqual(r.returncode, 1)
        self.assertIn('citation path "pkg/gone.go" not found', r.stderr)

    def test_unknown_repo(self):
        self.write_citation(self.header(repo="nobody-knows"), self.quoted())
        r = self.run_cli(self.write_answer(), "--check-citations",
                         "--repo", f"llm-d-router={self.cited}")
        self.assertEqual(r.returncode, 1)
        self.assertIn('c1: citation repo "nobody-knows" is unknown', r.stderr)

    def test_missing_header(self):
        self.write_citation(None, self.quoted())
        r = self.run_cli(self.write_answer(), "--check-citations",
                         "--repo", f"llm-d-router={self.cited}")
        self.assertEqual(r.returncode, 1)
        self.assertIn("c1: citation header missing or malformed", r.stderr)

    def test_malformed_header_bad_range(self):
        self.write_citation(self.header(start=4, end=3), self.quoted())
        r = self.run_cli(self.write_answer(), "--check-citations",
                         "--repo", f"llm-d-router={self.cited}")
        self.assertEqual(r.returncode, 1)
        self.assertIn("citation header missing or malformed", r.stderr)

    def test_bad_repo_flag_is_usage_error(self):
        self.write_citation(self.header(), self.quoted())
        r = self.run_cli(self.write_answer(), "--check-citations", "--repo", "nopath")
        self.assertEqual(r.returncode, 2)
        self.assertIn('bad --repo value "nopath"', r.stderr)

    def test_non_citation_evidence_ignored(self):
        (self.study / "notes.txt").write_text(VALUE + "\n")
        r = self.run_cli(self.write_answer(evidence="notes.txt"), "--check-citations")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("OK: 0 citations checked", r.stdout)

    # -- F4 resolution layers ---------------------------------------------
    def test_toplevel_remote_basename_resolution(self):
        # The study sits inside the cited repo, whose origin is .../llm-d-router.git.
        git(self.cited, "remote", "add", "origin",
            "git@github.com:llm-d/llm-d-router.git")
        study = self.cited / "investigations" / "s1"
        (study / "final").mkdir(parents=True)
        (study / "citations").mkdir()
        self.write_citation(self.header(), self.quoted(), study=study)
        r = self.run_cli(self.write_answer(study=study), "--check-citations")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("c1 citation OK", r.stdout)

    def test_toplevel_folder_name_when_no_remote(self):
        study = self.cited / "s1"
        (study / "final").mkdir(parents=True)
        (study / "citations").mkdir()
        self.write_citation(self.header(repo="checkout"), self.quoted(), study=study)
        r = self.run_cli(self.write_answer(study=study), "--check-citations")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_toplevel_remote_basename_mismatch_is_unknown(self):
        git(self.cited, "remote", "add", "origin", "https://github.com/x/other.git")
        study = self.cited / "s1"
        (study / "final").mkdir(parents=True)
        (study / "citations").mkdir()
        self.write_citation(self.header(), self.quoted(), study=study)
        r = self.run_cli(self.write_answer(study=study), "--check-citations")
        self.assertEqual(r.returncode, 1)
        self.assertIn('citation repo "llm-d-router" is unknown', r.stderr)

    def test_repo_flag_beats_toplevel(self):
        # The answer's toplevel matches the name by remote basename but is a
        # different repo (without self.sha); --repo must win.
        other = self.tmp / "other"
        other.mkdir()
        git(other, "init", "-q")
        (other / "README").write_text("unrelated\n")
        git(other, "add", ".")
        git(other, "commit", "-q", "-m", "unrelated")
        git(other, "remote", "add", "origin", "https://x/llm-d-router.git")
        study = other / "s1"
        (study / "final").mkdir(parents=True)
        (study / "citations").mkdir()
        self.write_citation(self.header(), self.quoted(), study=study)
        ans = self.write_answer(study=study)
        r = self.run_cli(ans, "--check-citations")
        self.assertEqual(r.returncode, 1)  # toplevel alone: sha not there
        self.assertIn("not found", r.stderr)
        r = self.run_cli(ans, "--check-citations", "--repo", f"llm-d-router={self.cited}")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_machine_config_repos_map(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed")
        (self.cfg_home / "config.yaml").write_text(
            f"repos:\n  llm-d-router: {self.cited}\n")
        self.write_citation(self.header(), self.quoted())
        r = self.run_cli(self.write_answer(), "--check-citations")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_bad_machine_config_is_noted_not_crash(self):
        (self.cfg_home / "config.yaml").write_text("repos: [unclosed\n")
        self.write_citation(self.header(), self.quoted())
        r = self.run_cli(self.write_answer(), "--check-citations")
        self.assertEqual(r.returncode, 1)
        self.assertIn('citation repo "llm-d-router" is unknown', r.stderr)
        self.assertIn("machine config skipped", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_missing_pyyaml_is_noted_not_crash(self):
        # Shadow PyYAML with a module that fails to import.
        shim = self.tmp / "shim"
        shim.mkdir()
        (shim / "yaml.py").write_text("raise ImportError('no yaml here')\n")
        (self.cfg_home / "config.yaml").write_text(f"repos:\n  llm-d-router: {self.cited}\n")
        self.write_citation(self.header(), self.quoted())
        env = {**os.environ, **GIT_ENV, "INVESTIGATION_CONFIG_HOME": str(self.cfg_home),
               "PYTHONPATH": str(shim)}
        r = subprocess.run([sys.executable, str(SCRIPT), str(self.write_answer()),
                            "--check-citations"], capture_output=True, text=True,
                           cwd=self.tmp, env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("machine config skipped: PyYAML is not installed", r.stderr)
        self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
