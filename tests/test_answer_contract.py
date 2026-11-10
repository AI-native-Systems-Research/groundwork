"""Pin the verbatim mlsys contract block in references/answer-contract.md.

The block is the run of blockquote ("> ") lines directly under the
"## The contract, verbatim" heading. It is copied word for word from the
mlsys study and must never drift; v2 additions go in sections below it.
If this test fails, the block was edited — restore it, don't update the hash.

Run from the repo root:  python3 -m unittest tests.test_answer_contract -v
"""
import hashlib
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONTRACT = REPO / "skills/investigation/references/answer-contract.md"
HEADING = "## The contract, verbatim"
VERBATIM_SHA256 = "d84f758d8f7356487d9baacf420cec228a9ee31b507cdf0576b779e29444c928"


def verbatim_block(text: str) -> str:
    """The contiguous '>' lines after HEADING (leading blank lines skipped),
    joined with newlines plus a final newline."""
    lines = text.split("\n")
    i = lines.index(HEADING) + 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    block = []
    while i < len(lines) and lines[i].startswith(">"):
        block.append(lines[i])
        i += 1
    return "\n".join(block) + "\n"


class AnswerContractTests(unittest.TestCase):
    def setUp(self):
        self.text = CONTRACT.read_text(encoding="utf-8")

    def test_verbatim_block_unchanged(self):
        block = verbatim_block(self.text)
        self.assertGreater(block.count("\n"), 10, "verbatim block not found")
        self.assertIn('"claims"', block)
        self.assertEqual(hashlib.sha256(block.encode("utf-8")).hexdigest(),
                         VERBATIM_SHA256)

    def test_v2_section_is_below_the_block(self):
        self.assertIn("## Evidence kinds (v2)", self.text)
        self.assertGreater(self.text.index("## Evidence kinds (v2)"),
                           self.text.index(HEADING))


if __name__ == "__main__":
    unittest.main()
