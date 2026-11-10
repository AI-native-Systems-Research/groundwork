"""Non-material templating replaces whole tokens only (R3 finding)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "investigation" / "scripts"))

from ains_runtime.preparation import template, template_bytes  # noqa: E402


class WholeTokenTemplating(unittest.TestCase):
    NM = {"run_id": "r1", "prep_dir": "/tmp/x/prep-1"}

    def test_short_value_inside_unrelated_path_is_kept(self):
        a = template(["/tmp/abr1x/data"], self.NM)
        self.assertEqual(a, ["/tmp/abr1x/data"])

    def test_whole_tokens_are_replaced(self):
        got = template(["--run-id", "r1", "/runs/r1/out", "/tmp/x/prep-1/c.yaml", "r1x"], self.NM)
        self.assertEqual(got, ["--run-id", "{run_id}", "/runs/{run_id}/out",
                               "{prep_dir}/c.yaml", "r1x"])

    def test_equivalent_inputs_digest_alike_despite_random_paths(self):
        a = template({"argv": ["--run-id", "r1", "/tmp/qr1z"]}, {"run_id": "r1"})
        b = template({"argv": ["--run-id", "r2", "/tmp/qr1z"]}, {"run_id": "r2"})
        self.assertEqual(a, b)

    def test_bytes(self):
        self.assertEqual(template_bytes(b"run_id: r1\np: /tmp/abr1x\n", self.NM),
                         b"run_id: {run_id}\np: /tmp/abr1x\n")


if __name__ == "__main__":
    unittest.main()
