"""Tests for validate_answer.py's guard and error branches, in-process.

tests/test_validate.py runs the CLI as a subprocess against fixtures and
unit-tests the contains-value helpers, but every guard that decides between
"report a precise error" and "crash with a traceback" was dead: the shape
guards (blank answer/deliverable/repro, non-object claims, blank ids,
missing/blank/directory evidence), the iron rule's OSError faces, and the
citation machinery's resolver refusals. These tests import validate_answer
directly (the house in-process pattern of test_doctor_inprocess.py /
test_collect_guards.py) and build minimal answer/study shapes in a tempdir,
so every guard branch counts without a subprocess.

Every test here pins behaviour a regression would silently break: an
unreadable evidence file must surface as "could not be read", not a
PermissionError traceback; a citation whose quote length differs from its
header span must say so, not IndexError.
"""
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "investigation" / "scripts"

sys.path.insert(0, str(SCRIPTS))
import validate_answer as va  # noqa: E402  (needs the sys.path line above)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(cwd, *args):
    r = subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                        "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main",
                        *args], cwd=cwd, env={**os.environ, **GIT_ENV},
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {args} failed: {r.stderr}")
    return r.stdout.strip()


def claim(**over):
    c = {"id": "c1", "metric": "m", "value": 1, "condition": "c", "evidence": "e.txt"}
    c.update(over)
    return c


def answer(claims, **over):
    d = {"answer": "a", "deliverable": None, "repro": "true", "claims": claims}
    d.update(over)
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.d = Path(self._td.name).resolve()

    def validate(self, data, root=None):
        return va.validate_answer(data, root or self.d)


class TopLevelShapeTest(Base):
    """validate_answer()'s top-level and claim shape guards."""

    def test_non_object_top_level(self):
        errors, _, _ = self.validate([1, 2, 3])
        self.assertEqual(errors, ["top level: must be a JSON object, got list"])

    def test_blank_answer(self):
        errors, _, _ = self.validate(answer([], answer="   "))
        self.assertIn("answer: must be a non-empty string", errors)

    def test_non_string_deliverable(self):
        errors, _, _ = self.validate(answer([], deliverable=5))
        self.assertIn("deliverable: must be null or a path string", errors)

    def test_blank_repro(self):
        errors, _, _ = self.validate(answer([], repro="   "))
        self.assertIn("repro: must be a non-empty string", errors)

    def test_repro_newline_warns_like_semicolon(self):
        # The 'a newline' arm of the warning ternary: same ONE-command warning.
        _, warnings, _ = self.validate(answer([], repro="cmd\nother"))
        self.assertIn("repro contains a newline", "".join(warnings))


class ClaimShapeTest(Base):
    """The per-claim shape guards and their error labels."""

    def test_claim_not_an_object_uses_the_index_label(self):
        errors, _, _ = self.validate(answer(["not a dict"]))
        self.assertIn("claims[0]: must be an object with exactly the keys "
                      "id, metric, value, condition, evidence", errors)

    def test_blank_id_falls_back_to_the_index_label(self):
        # A blank id keeps the claims[i] label for the rest of the claim's
        # errors instead of an empty label.
        (self.d / "e.txt").write_text("nothing here")
        errors, _, _ = self.validate(answer([claim(id="  ", evidence="e.txt")]))
        self.assertIn("claims[0]: id must be a short unique non-empty label", errors)
        self.assertIn("claims[0]: evidence file does not contain value 1", errors)

    def test_missing_evidence_key_stops_at_the_missing_key_error(self):
        # No evidence key: the iron rule is skipped (the missing-key error
        # already failed the claim), so no "evidence file does not exist".
        c = claim()
        del c["evidence"]
        errors, _, _ = self.validate(answer([c]))
        self.assertEqual([f'c1: missing key "evidence"'], errors)

    def test_blank_evidence(self):
        errors, _, _ = self.validate(answer([claim(evidence="   ")]))
        self.assertEqual(["c1: evidence must be a non-empty path string"], errors)

    def test_evidence_is_a_directory(self):
        (self.d / "adir").mkdir()
        errors, _, _ = self.validate(answer([claim(evidence="adir")]))
        self.assertEqual(["c1: evidence path is not a file: "
                          f"{self.d / 'adir'}"], errors)


class EvidenceIOTest(Base):
    """The iron rule's I/O guards: unreadable files degrade to errors."""

    def setUp(self):
        super().setUp()
        if os.geteuid() == 0:
            self.skipTest("running as root: permission bits not enforced")

    def test_unreadable_evidence_reports_the_read_error(self):
        p = self.d / "e.txt"
        p.write_text("1")
        p.chmod(0)
        self.addCleanup(p.chmod, stat.S_IRUSR | stat.S_IWUSR)
        errors, _, _ = self.validate(answer([claim(evidence="e.txt")]))
        self.assertEqual(1, len(errors))
        self.assertIn("c1: evidence file could not be read:", errors[0])
        self.assertIn("Permission denied", errors[0])

    def test_small_file_read_oserror_surfaces_as_an_error_string(self):
        p = self.d / "e.txt"
        p.write_text("1")
        p.chmod(0)
        self.addCleanup(p.chmod, stat.S_IRUSR | stat.S_IWUSR)
        found, err = va.file_contains(p, 1)
        self.assertFalse(found)
        self.assertIn("Permission denied", err)

    def test_large_file_open_oserror_surfaces_as_an_error_string(self):
        # A file over LARGE_FILE_BYTES takes the line-scan path; an open()
        # failure there is reported, not raised. Sparse, so it is instant.
        p = self.d / "big.txt"
        with open(p, "wb") as f:
            f.truncate(va.LARGE_FILE_BYTES + 1024)
        p.chmod(0)
        self.addCleanup(p.chmod, stat.S_IRUSR | stat.S_IWUSR)
        found, err = va.file_contains(p, 1)
        self.assertFalse(found)
        self.assertIn("Permission denied", err)

    def test_structural_search_degrades_to_text_on_bad_json(self):
        # An evidence file that starts with '{' but is not valid JSON: the
        # structural search gives up and the text search decides.
        (self.d / "e.json").write_text("{not valid json")
        errors, _, _ = self.validate(answer([claim(evidence="e.json")]))
        self.assertEqual(["c1: evidence file does not contain value 1"], errors)


class IronRuleHelperTest(Base):
    def test_standalone_number_in_with_no_forms_is_false(self):
        # The empty-forms guard: no spellings can never match.
        self.assertFalse(va.standalone_number_in("any text at all", []))


class CitationPathTest(Base):
    def test_absolute_path_outside_the_root_is_not_a_citation(self):
        self.assertFalse(va.is_citation_path("/elsewhere/citations/x.md", self.d))

    def test_absolute_path_inside_the_root_is_a_citation(self):
        self.assertTrue(va.is_citation_path(str(self.d / "citations" / "x.md"), self.d))


class CitationCheckTest(Base):
    """check_citation()'s refusal faces, driven in-process with a real
    throwaway git repo (the make_repo pattern of test_validate_citations.py)."""

    def setUp(self):
        super().setUp()
        self.repo = self.d / "checkout"
        self.repo.mkdir()
        (self.repo / "doc.md").write_text("line one\nline two\nline three\n")
        git(self.repo, "init", "-q")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "init")
        self.sha = git(self.repo, "rev-parse", "HEAD")
        self.study = self.d / "study"
        (self.study / "citations").mkdir(parents=True)

    def write_citation(self, header, body, name="c1.md"):
        p = self.study / "citations" / name
        p.write_text(header + "\n" + body)
        return p

    def test_unreadable_citation_file(self):
        if os.geteuid() == 0:
            self.skipTest("running as root: permission bits not enforced")
        p = self.write_citation(f"repo@{self.sha}:doc.md:L1-L1", "line one\n")
        p.chmod(0)
        self.addCleanup(p.chmod, stat.S_IRUSR | stat.S_IWUSR)
        resolver = va.RepoResolver({"repo": self.repo}, p)
        err = va.check_citation("c1", p, resolver)
        self.assertIn("c1: citation file could not be read:", err)
        self.assertIn("Permission denied", err)

    def test_resolved_repo_path_does_not_exist(self):
        p = self.write_citation(f"repo@{self.sha}:doc.md:L1-L1", "line one\n")
        resolver = va.RepoResolver({"repo": self.d / "missing"}, p)
        err = va.check_citation("c1", p, resolver)
        self.assertEqual(f'c1: citation repo "repo" path does not exist: '
                         f"{self.d / 'missing'}", err)

    def test_resolved_repo_is_not_a_git_repository(self):
        notgit = self.d / "notgit"
        notgit.mkdir()
        p = self.write_citation(f"repo@{self.sha}:doc.md:L1-L1", "line one\n")
        resolver = va.RepoResolver({"repo": notgit}, p)
        err = va.check_citation("c1", p, resolver)
        self.assertEqual(f'c1: citation repo "repo" is not a git repository: '
                         f"{notgit}", err)

    def test_quote_shorter_than_the_header_span_names_both_counts(self):
        # The header spans three lines but one was quoted: the mismatch must
        # carry the "; quoted N lines, header spans M" suffix, not an
        # IndexError or a bare mismatch.
        p = self.write_citation(f"repo@{self.sha}:doc.md:L1-L3", "line one\n")
        resolver = va.RepoResolver({"repo": self.repo}, p)
        err = va.check_citation("c1", p, resolver)
        self.assertIn("quoted text differs from the source", err)
        self.assertIn("quoted 1 lines, header spans 3", err)

    def test_git_unavailable_is_an_error_not_a_crash(self):
        # _git_bytes' OSError face: git missing from PATH entirely.
        p = self.write_citation(f"repo@{self.sha}:doc.md:L1-L1", "line one\n")
        resolver = va.RepoResolver({"repo": self.repo}, p)
        real_run = va.subprocess.run

        def no_git(argv, **kw):
            if argv and argv[0] == "git":
                raise OSError("git not found")
            return real_run(argv, **kw)

        va.subprocess.run = no_git
        self.addCleanup(setattr, va.subprocess, "run", real_run)
        rc, out, err = va._git_bytes(self.repo, "rev-parse", "--git-dir")
        self.assertEqual(rc, -1)
        self.assertEqual(out, b"")
        self.assertIn("git not found", err)


class CheckCitationsSkipsTest(Base):
    """check_citations() skips rows it cannot treat as citations."""

    def test_claims_not_a_list_checks_nothing(self):
        errors, checked = va.check_citations({"claims": "nope"}, self.d, None)
        self.assertEqual((errors, checked), ([], []))

    def test_non_object_claim_rows_are_skipped(self):
        errors, checked = va.check_citations({"claims": ["not a dict", 7]}, self.d, None)
        self.assertEqual((errors, checked), ([], []))

    def test_non_string_evidence_is_skipped(self):
        data = {"claims": [{"id": "c1", "evidence": 5}]}
        errors, checked = va.check_citations(data, self.d, None)
        self.assertEqual((errors, checked), ([], []))

    def test_missing_citation_file_is_skipped(self):
        # A citation-path evidence whose file is absent: the contract layer
        # already reports it; the citation check must skip, not crash.
        (self.d / "citations").mkdir()
        data = {"claims": [{"id": "c1", "evidence": "citations/gone.md"}]}
        errors, checked = va.check_citations(data, self.d, None)
        self.assertEqual((errors, checked), ([], []))


class RepoResolverFallbackTest(Base):
    """RepoResolver's degraded layers: ImportError and a non-mapping repos:."""

    def setUp(self):
        super().setUp()
        (self.d / "answer.json").write_text("{}")
        self.saved_path = list(sys.path)
        self.saved_cfg = os.environ.pop("INVESTIGATION_CONFIG_HOME", None)
        self.addCleanup(self._restore)

    def _restore(self):
        sys.path[:] = self.saved_path
        if self.saved_cfg is not None:
            os.environ["INVESTIGATION_CONFIG_HOME"] = self.saved_cfg

    def test_toplevel_import_failure_degrades_to_unknown(self):
        r = va.RepoResolver({}, self.d / "answer.json")
        real = va._import_config
        va._import_config = lambda: (_ for _ in ()).throw(ImportError("no _config"))
        self.addCleanup(setattr, va, "_import_config", real)
        self.assertEqual(r._toplevel(), (None, None))

    def test_config_repos_import_failure_is_a_note(self):
        r = va.RepoResolver({}, self.d / "answer.json")
        real = va._import_config
        va._import_config = lambda: (_ for _ in ()).throw(ImportError("no _config"))
        self.addCleanup(setattr, va, "_import_config", real)
        repos, note = r._config_repos()
        self.assertEqual(repos, {})
        self.assertIn("machine config skipped", note)

    def test_config_repos_not_a_mapping_is_a_note(self):
        cfg = self.d / "cfg"
        cfg.mkdir()
        (cfg / "config.yaml").write_text("repos: [a, b]\n")
        os.environ["INVESTIGATION_CONFIG_HOME"] = str(cfg)
        r = va.RepoResolver({}, self.d / "answer.json")
        repos, note = r._config_repos()
        self.assertEqual(repos, {})
        self.assertIn("repos must be a mapping of name: path", note)

    def test_import_config_inserts_the_scripts_dir_when_absent(self):
        # _import_config must work when the scripts dir is not already on
        # sys.path (the CLI's subprocess entry relies on this only implicitly).
        sys.path = [p for p in sys.path if str(SCRIPTS) not in p]
        cfg = va._import_config()
        self.assertEqual(cfg.__name__, "_config")
        self.assertIn(str(SCRIPTS), sys.path)


class MainUnreadableAnswerTest(Base):
    def test_unreadable_answer_file_is_exit_2_not_a_traceback(self):
        if os.geteuid() == 0:
            self.skipTest("running as root: permission bits not enforced")
        p = self.d / "answer.json"
        p.write_text("{}")
        p.chmod(0)
        self.addCleanup(p.chmod, stat.S_IRUSR | stat.S_IWUSR)
        rc = va.main([str(p)])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
