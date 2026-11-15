"""Branch coverage for _collection.py — the shared collection.json merge.

Measured on main 8d40c69 (CI run 43504194546, unit 3.12) at 83%: the
existing coverage is behavioural-through-CLIs only (test_collect.py,
test_collect_telemetry.py, test_collect_spotlights.py run the collectors
as subprocesses and never import the helper), so the helper's own guard
branches read as uncovered:

  - read_collection's OSError branch (unreadable, not absent)
  - read_collection's invalid-JSON branch (refused, never overwritten)
  - update_collection's crash cleanup (temp file unlinked, original kept)
"""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills/investigation/scripts"))
import _collection as C  # noqa: E402


class ReadCollectionGuards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.study = Path(self.tmp.name)

    def test_unreadable_file_is_an_error_not_absent(self):
        # A file that exists but can't be read must raise CollectionError;
        # falling through to "absent -> {}" would silently drop the study's
        # collection and let a merge overwrite it.
        p = self.study / "collection.json"
        p.write_text('{"collection_version": 2}')
        os.chmod(p, 0)
        self.addCleanup(os.chmod, p, stat.S_IRUSR | stat.S_IWUSR)
        if os.access(p, os.R_OK):            # running as root: permission bits off
            self.skipTest("chmod 0 does not block reads as this user")
        with self.assertRaisesRegex(C.CollectionError, "unreadable"):
            C.read_collection(self.study)

    def test_invalid_json_is_refused_never_overwritten(self):
        # Malformed text (not the JSON-array case the CLI tests cover): the
        # helper refuses and the file keeps its exact bytes.
        p = self.study / "collection.json"
        p.write_text("{nope")
        with self.assertRaisesRegex(C.CollectionError, "not valid JSON"):
            C.read_collection(self.study)
        with self.assertRaisesRegex(C.CollectionError, "not valid JSON"):
            C.merge_section(self.study, "nous", {"run_id": "x"})
        self.assertEqual(p.read_text(), "{nope")
        self.assertEqual(list(self.study.iterdir()), [p])   # no temp file left


class UpdateCollectionCrashCleanup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.study = Path(self.tmp.name)
        (self.study / "collection.json").write_text(json.dumps(
            {"collection_version": 2, "nous": {"run_id": "keep-me"}}))

    def test_failed_write_cleans_the_temp_and_keeps_the_original(self):
        # os.replace failing (or the write raising) must unlink the
        # .collection.*.tmp file, keep the old file intact, and re-raise:
        # the docstring's "a crash leaves the old file" promise.
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                C.merge_section(self.study, "nous", {"run_id": "new"})
        leftovers = [p for p in self.study.iterdir() if p.name != "collection.json"]
        self.assertEqual(leftovers, [])
        self.assertEqual(
            json.loads((self.study / "collection.json").read_text())["nous"],
            {"run_id": "keep-me"})

    def test_unlink_failure_during_cleanup_does_not_mask_the_error(self):
        # The cleanup unlink is best-effort: an OSError from os.unlink must
        # not replace the original exception.
        real_unlink = os.unlink

        def flaky_unlink(path, *a, **kw):
            if str(path).endswith(".tmp"):
                raise OSError("unlink failed too")
            return real_unlink(path, *a, **kw)

        with mock.patch("os.replace", side_effect=OSError("disk full")), \
                mock.patch("os.unlink", side_effect=flaky_unlink):
            # assertRaisesRegex, not assertRaises: a bare OSError also
            # passes if the cleanup's own failure replaces the original
            # error, which is exactly what this test exists to forbid.
            with self.assertRaisesRegex(OSError, "disk full"):
                C.merge_section(self.study, "nous", {"run_id": "new"})
        self.assertEqual(
            json.loads((self.study / "collection.json").read_text())["nous"],
            {"run_id": "keep-me"})
