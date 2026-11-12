"""Tests for doctor.py's engine-discovery parsers (unit level).

latest_release, installed_info and engine_info decide whether an installed
engine matches its pin — the warnings the skill acts on before any governed
launch. They were only reachable end-to-end with real engines installed, so
these tests drive them directly:

  * latest_release    against patched `git ls-remote --tags` output
  * installed_info    against a fake venv layout (dist-info METADATA +
                      direct_url.json) in a tempdir
  * engine_info       against throwaway git checkouts (same pattern as
                      test_doctor.py, which drives the CLI in a subprocess;
                      these pin the per-branch behaviour the JSON tests only
                      see in aggregate)

No network: latest_release's git call is patched, and engine_info runs with
offline=True.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "investigation/scripts"

sys.path.insert(0, str(SCRIPTS))
import doctor  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
           "GIT_CONFIG_NOSYSTEM": "1"}


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


LS_REMOTE = "\n".join([
    "aaa111\trefs/tags/v0.9.0",
    "bbb222\trefs/tags/v1.2.3",
    "ccc333\trefs/tags/v1.2.3^{}",
    "ddd444\trefs/tags/release-x",   # not a version tag: ignored
    "eee555\trefs/heads/main",       # not a tag: ignored
    "fff666\trefs/tags/10.0.0",      # version-ordering, not string ordering
])


class LatestRelease(unittest.TestCase):
    """latest_release: pick the highest vX.Y.Z tag from ls-remote output."""

    def test_none_when_ls_remote_fails(self):
        with mock.patch.object(doctor, "run", return_value=None):
            self.assertIsNone(doctor.latest_release("https://x"))

    def test_picks_the_highest_version_ignoring_non_version_refs(self):
        with mock.patch.object(doctor, "run", return_value=LS_REMOTE):
            self.assertEqual(doctor.latest_release("https://x"),
                             {"tag": "10.0.0", "sha": "fff666"})

    def test_peeled_tag_sha_wins_over_the_tag_object(self):
        out = ("aaa111\trefs/tags/v1.0.0\n"
               "bbb222\trefs/tags/v1.0.0^{}\n")
        with mock.patch.object(doctor, "run", return_value=out):
            self.assertEqual(doctor.latest_release("https://x"),
                             {"tag": "v1.0.0", "sha": "bbb222"})

    def test_no_tags_gives_none_tag(self):
        with mock.patch.object(doctor, "run", return_value="zzz\trefs/heads/main"):
            self.assertEqual(doctor.latest_release("https://x"),
                             {"tag": None, "sha": None})

    def test_v_prefix_is_optional(self):
        out = "aaa111\trefs/tags/2.5\nbbb222\trefs/tags/v2.4\n"
        with mock.patch.object(doctor, "run", return_value=out):
            self.assertEqual(doctor.latest_release("https://x")["tag"], "2.5")


class InstalledInfo(unittest.TestCase):
    """installed_info: read an installed tool's version/sha from its venv."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.venv = self.tmp / "venv"
        self.site = self.venv / "lib" / "python3.13" / "site-packages"
        interp = self.venv / "bin" / "python"
        interp.parent.mkdir(parents=True)
        interp.write_text("#!/bin/sh\n")
        self.exe = self.tmp / "tool"
        self.exe.write_text(f"#!{interp}\n")

    def dist_info(self, version: str, direct_url: dict) -> Path:
        di = self.site / f"fakedist-{version}.dist-info"
        di.mkdir(parents=True)
        (di / "METADATA").write_text(f"Name: fakedist\nVersion: {version}\n")
        (di / "direct_url.json").write_text(json.dumps(direct_url))
        return di

    def test_no_dist_name_gives_empty_info(self):
        info = doctor.installed_info(str(self.exe), None)
        self.assertEqual(info["version"], None)
        self.assertEqual(info["sha"], None)
        self.assertEqual(info["path"], str(self.exe))

    def test_no_shebang_gives_empty_info(self):
        plain = self.tmp / "plain"
        plain.write_text("print('x')\n")
        info = doctor.installed_info(str(plain), "fakedist")
        self.assertIsNone(info["version"])

    def test_vcs_install_reads_version_sha_and_ref(self):
        self.dist_info("1.2.3", {"url": "https://github.com/org/repo.git",
                                 "vcs_info": {"commit_id": "a" * 40,
                                              "requested_revision": "main"}})
        info = doctor.installed_info(str(self.exe), "fakedist")
        self.assertEqual(info["version"], "1.2.3")
        self.assertEqual(info["sha"], "a" * 40)
        self.assertEqual(info["ref"], "main")
        self.assertEqual(info["upstream_url"], "https://github.com/org/repo.git")

    def test_highest_dist_info_version_wins(self):
        self.dist_info("1.2.3", {})
        self.dist_info("2.0.0", {})
        info = doctor.installed_info(str(self.exe), "fakedist")
        self.assertEqual(info["version"], "2.0.0")

    def test_highest_dist_info_version_wins_numerically(self):
        # 'fakedist-1.9.0.dist-info' sorts AFTER 'fakedist-1.10.0.dist-info'
        # lexicographically ('9' > '1'), so the pre-fix last-hit pick reported
        # the OLDER install. Distinct commit ids make the wrong pick visible
        # in the sha and requested_revision too, not just the version.
        self.dist_info("1.9.0", {"url": "https://github.com/org/repo.git",
                                 "vcs_info": {"commit_id": "a" * 40,
                                              "requested_revision": "v1.9.0"}})
        self.dist_info("1.10.0", {"url": "https://github.com/org/repo.git",
                                  "vcs_info": {"commit_id": "b" * 40,
                                               "requested_revision": "v1.10.0"}})
        info = doctor.installed_info(str(self.exe), "fakedist")
        self.assertEqual(info["version"], "1.10.0")
        self.assertEqual(info["ref"], "v1.10.0")
        self.assertEqual(info["sha"], "b" * 40)

    def test_editable_file_url_reads_the_checkout(self):
        head = make_repo(self.tmp / "repo")
        self.dist_info("3.0.0", {"url": f"file://{self.tmp / 'repo'}",
                                 "dir_info": {"editable": True}})
        info = doctor.installed_info(str(self.exe), "fakedist")
        # The checkout's git facts replace the dist-info version (describe
        # falls back to the short sha when the checkout has no tags).
        self.assertEqual(info["version"], head[:7])
        self.assertEqual(info["sha"], head)
        self.assertEqual(info["ref"], "main")
        self.assertEqual(info["upstream_url"], "https://github.com/org/repo.git")
        self.assertFalse(info["pushed"])  # HEAD is on no remote branch

    def test_unparseable_direct_url_leaves_fields_empty(self):
        di = self.dist_info("1.0.0", {})
        (di / "direct_url.json").write_text("not json")
        info = doctor.installed_info(str(self.exe), "fakedist")
        self.assertEqual(info["version"], "1.0.0")
        self.assertIsNone(info["sha"])

    def test_unreadable_exe_gives_empty_info(self):
        info = doctor.installed_info(str(self.tmp / "not-there"), "fakedist")
        self.assertIsNone(info["version"])
        self.assertIsNone(info["sha"])
        self.assertEqual(info["path"], str(self.tmp / "not-there"))

    def test_empty_venv_gives_empty_info(self):
        # A shebang pointing at a venv without the expected dist.
        venv2 = self.tmp / "empty-venv"
        interp2 = venv2 / "bin" / "python"
        interp2.parent.mkdir(parents=True)
        interp2.write_text("#!/bin/sh\n")
        exe2 = self.tmp / "other-tool"
        exe2.write_text(f"#!{interp2}\n")
        info = doctor.installed_info(str(exe2), "fakedist")
        self.assertIsNone(info["version"])

    def test_missing_metadata_leaves_version_unset(self):
        di = self.site / "fakedist-1.0.0.dist-info"
        di.mkdir(parents=True)  # no METADATA inside
        info = doctor.installed_info(str(self.exe), "fakedist")
        self.assertIsNone(info["version"])


class EngineInfo(unittest.TestCase):
    """engine_info: config checkouts, PATH installs, missing engines, pins."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def engine(self, name, machine, pins=None, offline=True, cache=None):
        return doctor.engine_info(name, machine, pins or {}, offline, cache if cache is not None else {})

    def test_configured_checkout_is_found_with_git_facts(self):
        head = make_repo(self.tmp / "eng")
        e = self.engine("nous", {"engines": {"nous": str(self.tmp / "eng")}})
        self.assertTrue(e["found"])
        self.assertEqual(e["source"], "config")
        self.assertEqual(e["sha"], head)
        self.assertEqual(e["ref"], "main")

    def test_configured_path_that_does_not_exist_warns(self):
        e = self.engine("nous", {"engines": {"nous": str(self.tmp / "nope")}})
        self.assertFalse(e["found"])
        self.assertTrue(any("does not exist" in w for w in e["warnings"]))

    def test_unconfigured_engine_not_on_path_is_not_found(self):
        e = self.engine("sim2real", {})
        self.assertFalse(e["found"])
        self.assertIsNone(e["source"])

    def test_signal_pipeline_falls_back_to_spotlights_engine_config(self):
        eng = self.tmp / "eng"
        head = make_repo(eng)
        e = self.engine("signal-pipeline", {"engines": {"spotlights-engine": str(eng)}})
        self.assertTrue(e["found"])
        self.assertEqual(e["sha"], head)
        # ...and warns when the checkout lacks the signal_pipeline package.
        self.assertTrue(any("has no src/spotlights_engine/signal_pipeline" in w
                            for w in e["warnings"]))
        (eng / "src" / "spotlights_engine" / "signal_pipeline").mkdir(parents=True)
        e = self.engine("signal-pipeline", {"engines": {"spotlights-engine": str(eng)}})
        self.assertFalse(any("signal_pipeline" in w for w in e["warnings"]))

    def test_matching_pin_gives_no_mismatch_warning(self):
        head = make_repo(self.tmp / "eng")
        e = self.engine("nous", {"engines": {"nous": str(self.tmp / "eng")}},
                        {"nous-pin": f"main@{head[:7]}"})
        self.assertFalse(any("differs from the pin" in w for w in e["warnings"]))

    def test_pin_mismatch_warns_with_installed_and_pin(self):
        make_repo(self.tmp / "eng")
        e = self.engine("nous", {"engines": {"nous": str(self.tmp / "eng")}},
                        {"nous-pin": "main@0000000"})
        self.assertTrue(any("differs from the pin main@0000000" in w
                            for w in e["warnings"]))

    def test_unpushed_head_warns(self):
        make_repo(self.tmp / "eng")  # no remote branch contains HEAD
        e = self.engine("nous", {"engines": {"nous": str(self.tmp / "eng")}})
        self.assertTrue(any("not on any remote branch" in w for w in e["warnings"]))

    def test_path_install_is_found_with_installed_facts(self):
        with mock.patch.object(doctor.shutil, "which", return_value="/fake/bin/nous"), \
             mock.patch.object(doctor, "installed_info",
                               return_value={"path": "/fake/bin/nous", "sha": "a" * 40,
                                             "version": "1.0.0", "ref": None,
                                             "upstream_url": None, "pushed": None}):
            e = self.engine("nous", {})
        self.assertTrue(e["found"])
        self.assertEqual(e["source"], "PATH")
        self.assertEqual(e["sha"], "a" * 40)

    def test_pin_with_unreadable_sha_warns(self):
        with mock.patch.object(doctor.shutil, "which", return_value="/fake/bin/nous"), \
             mock.patch.object(doctor, "installed_info",
                               return_value={"path": "/fake/bin/nous", "sha": None,
                                             "version": None, "ref": None,
                                             "upstream_url": None, "pushed": None}):
            e = self.engine("nous", {}, {"nous-pin": "main@0000000"})
        self.assertEqual(e["warnings"], ["can't read the installed sha to compare with pin main@0000000"])

    def test_online_check_reports_a_stale_pin(self):
        head = make_repo(self.tmp / "eng")
        machine = {"engines": {"nous": str(self.tmp / "eng")}}
        rel = {"tag": "v9.9.9", "sha": "b" * 40}
        with mock.patch.object(doctor, "latest_release", return_value=rel):
            e = self.engine("nous", machine, {"nous-pin": f"v1.0.0@{'a' * 7}"}, offline=False)
        self.assertEqual(e["latest_release"], rel)
        self.assertTrue(e["latest_checked"])
        self.assertTrue(any("differs from the latest upstream release v9.9.9" in w
                            for w in e["warnings"]))
        self.assertTrue(any("pin v1.0.0@aaaaaaa is not the latest release v9.9.9" in w
                            for w in e["warnings"]))
        # The pin matches the checkout head, so only staleness (not drift) is flagged.
        with mock.patch.object(doctor, "latest_release", return_value=rel):
            fresh = self.engine("nous", machine, {"nous-pin": f"main@{head[:7]}"},
                                offline=False)
        self.assertFalse(any("differs from the pin" in w for w in fresh["warnings"]))
        self.assertFalse(any("is not the latest release" in w for w in fresh["warnings"]))

    def test_online_release_lookup_is_cached_per_url(self):
        make_repo(self.tmp / "eng")
        machine = {"engines": {"nous": str(self.tmp / "eng")}}
        cache = {}
        rel = {"tag": "v9.9.9", "sha": "b" * 40}
        with mock.patch.object(doctor, "latest_release", return_value=rel) as lr:
            for _ in range(2):
                self.engine("nous", machine, offline=False, cache=cache)
        self.assertEqual(lr.call_count, 1)


if __name__ == "__main__":
    unittest.main()
