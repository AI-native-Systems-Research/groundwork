"""upstreams.py: the lock-integrity refusal faces no suite reaches.

test_upstreams.py pins the happy contract — every facts file verified
against its lock entry, the representability table, no restated versions —
but it only ever reads the real, valid references/upstreams/ tree. Measured
on main 8d40c69 (CI run 43504194546, unit 3.12; reproduced locally with
.coveragerc subprocess tracing, full-suite discover + combine) the module
reads 91% with exactly 6 statements never reached, every one a refusal:

  - _read's OSError/ValueError wrap (35-36) — a missing or malformed file
    must surface as UpstreamError, not a bare traceback, because doctor
    imports this module (it is the "stdlib only" leaf)
  - _read's non-object refusal (38) — valid JSON that is not an object
  - lock's no-`upstreams`-map refusal (47) — a lock without the map
  - entry's unknown-name refusal (54) — the face every adapter hits when
    a name drifts between the lock and the code that cites it
  - fact's missing-key refusal (84) — a cited fact key that is not in the
    facts file

These tests drive each face in-process against temp lock files (the
lock(path) cache is keyed by path, so a fresh temp path never disturbs the
real LOCK_FILE entry) and against the real lock for the name/key faces:
no subprocess, no environment scrubbing, no cache monkeypatching.
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "investigation"
sys.path.insert(0, str(SKILL / "scripts"))

import upstreams as U  # noqa: E402


class TempLockFaces(unittest.TestCase):
    """A throwaway lock.json per test: the file-shape refusals."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_missing_file_is_upstream_error(self):
        # The OSError face: a path that does not exist must wrap, not
        # leak the FileNotFoundError to doctor or the adapters.
        missing = self.tmp / "nope.json"
        with self.assertRaises(U.UpstreamError) as cm:
            U.lock(missing)
        self.assertIn(str(missing), str(cm.exception))
        self.assertIn("No such file", str(cm.exception))

    def test_malformed_json_is_upstream_error(self):
        # The ValueError face: json.loads' error text must ride along so
        # the operator can see what is wrong with the file.
        p = self.tmp / "lock.json"
        p.write_text("{not json", encoding="utf-8")
        with self.assertRaises(U.UpstreamError) as cm:
            U.lock(p)
        self.assertIn(str(p), str(cm.exception))

    def test_non_object_top_level_is_refused(self):
        # Valid JSON, wrong shape: the lock must be an object.
        p = self.tmp / "lock.json"
        p.write_text("[1, 2]", encoding="utf-8")
        with self.assertRaisesRegex(U.UpstreamError, "not a JSON object"):
            U.lock(p)

    def test_lock_without_upstreams_map_is_refused(self):
        # An object with no `upstreams` key is not a lock.
        p = self.tmp / "lock.json"
        p.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(U.UpstreamError, "no `upstreams` map"):
            U.lock(p)

    def test_lock_with_empty_upstreams_map_is_refused(self):
        # `upstreams: {}` is falsy: a lock that locks nothing is refused,
        # the same as a missing map — silent acceptance would read as
        # "there are no upstreams" everywhere entry() is called.
        p = self.tmp / "lock.json"
        p.write_text('{"upstreams": {}}', encoding="utf-8")
        with self.assertRaisesRegex(U.UpstreamError, "no `upstreams` map"):
            U.lock(p)

    def test_lock_with_non_mapping_upstreams_is_refused(self):
        # `upstreams` must itself be a mapping, not a list.
        p = self.tmp / "lock.json"
        p.write_text('{"upstreams": [1]}', encoding="utf-8")
        with self.assertRaisesRegex(U.UpstreamError, "no `upstreams` map"):
            U.lock(p)

    def test_lock_with_a_map_loads(self):
        # The happy face on the same temp-path route: the map is returned
        # as-is, so the refusal tests above are known to be refusals.
        p = self.tmp / "lock.json"
        p.write_text('{"upstreams": {"x": {"commit": "a"}}}',
                     encoding="utf-8")
        self.assertEqual(U.lock(p), {"x": {"commit": "a"}})


class RealLockFaces(unittest.TestCase):
    """The name/key faces against the real lock (no files touched)."""

    def test_unknown_upstream_name_is_refused(self):
        # entry() is what every adapter and test calls; a name that is
        # not in the lock must refuse with the name and the lock path,
        # not KeyError.
        with self.assertRaises(U.UpstreamError) as cm:
            U.entry("no-such-upstream")
        self.assertIn("no upstream 'no-such-upstream'", str(cm.exception))
        self.assertIn(str(U.LOCK_FILE), str(cm.exception))

    def test_unknown_fact_key_is_refused(self):
        # fact() is how the runtime cites upstream facts; a key that is
        # not in the facts file must refuse naming the file and the key.
        with self.assertRaises(U.UpstreamError) as cm:
            U.fact("nous", "no-such-fact")
        self.assertIn("no fact 'no-such-fact'", str(cm.exception))
        self.assertIn(U.entry("nous")["facts"], str(cm.exception))


if __name__ == "__main__":
    unittest.main()
