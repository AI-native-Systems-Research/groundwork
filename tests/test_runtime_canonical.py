"""ains_runtime.canonical: canonical JSON and digests are stable."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _runtime_util  # noqa: F401,E402  (sets sys.path)
from ains_runtime import canonical as C  # noqa: E402


class CanonicalJson(unittest.TestCase):
    def test_key_order_does_not_matter(self):
        a = {"b": 1, "a": {"y": [1, 2, {"q": None, "p": True}], "x": "s"}}
        b = json.loads('{"a": {"x": "s", "y": [1, 2, {"p": true, "q": null}]}, "b": 1}')
        self.assertEqual(C.canonical_json(a), C.canonical_json(b))
        self.assertEqual(C.digest(a), C.digest(b))
        self.assertEqual(C.canonical_json(a),
                         '{"a":{"x":"s","y":[1,2,{"p":true,"q":null}]},"b":1}')

    def test_unicode_is_utf8_not_escaped_and_escape_spelling_is_irrelevant(self):
        raw = '{"q": "caf\\u00e9 \\u2014 \\ud83d\\ude80"}'
        lit = '{"q": "café — 🚀"}'
        a, b = C.loads_strict(raw), C.loads_strict(lit)
        self.assertEqual(C.canonical_bytes(a), C.canonical_bytes(b))
        self.assertEqual(C.canonical_json(a), '{"q":"café — 🚀"}')
        self.assertIn("é".encode("utf-8"), C.canonical_bytes(a))

    def test_unicode_keys_sort_stably(self):
        a = {"é": 1, "e": 2, "z": 3, "Z": 4}
        b = dict(reversed(list(a.items())))
        self.assertEqual(C.canonical_json(a), C.canonical_json(b))

    def test_no_unicode_normalization(self):
        # NFC and NFD spellings are different content.
        self.assertNotEqual(C.digest({"q": "é"}), C.digest({"q": "é"}))

    def test_known_digest(self):
        self.assertEqual(
            C.digest({}),
            "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a")

    def test_short_code(self):
        d = C.digest({"x": 1})
        self.assertEqual(C.short_code(d), d[7:19])
        self.assertEqual(len(C.short_code(d)), 12)

    def test_rejects_non_finite_and_bad_types(self):
        for bad in (float("nan"), float("inf"), {"a": {1, 2}}, {1: "x"}, b"x"):
            with self.assertRaises(C.CanonicalError):
                C.canonical_json(bad)

    def test_loads_strict_rejects_duplicate_keys_and_constants(self):
        with self.assertRaises(C.CanonicalError):
            C.loads_strict('{"a": 1, "a": 2}')
        with self.assertRaises(C.CanonicalError):
            C.loads_strict('{"a": NaN}')
        with self.assertRaises(C.CanonicalError):
            C.loads_strict('{"a": ')

    def test_material_digest_requires_mapping(self):
        with self.assertRaises(C.CanonicalError):
            C.material_digest(["x"])
        self.assertEqual(C.material_digest({"a": 1}), C.digest({"a": 1}))


if __name__ == "__main__":
    unittest.main()
