"""Tests for scripts/validate_answer.py — the answer-contract validator.

Two layers, on purpose:

  * subprocess tests run the real CLI against the fixtures in
    tests/fixtures/ (one per failure mode plus the valid case) and assert
    exit codes and the key stderr messages — exactly how the skill and the
    owner will run it;
  * unit tests import the module and exercise the contains-value helpers
    directly — the iron rule's tolerances, the int-vs-float spellings, the
    bool exclusion and the large-file line-by-line path.

Run from the repo root:  python3 -m unittest tests.test_validate -v
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills/investigation/scripts" / "validate_answer.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

sys.path.insert(0, str(REPO / "skills/investigation/scripts"))
import validate_answer as va  # noqa: E402  (needs the sys.path line above)


def run_validator(answer_path, *extra):
    """Run the CLI as a subprocess; returns the CompletedProcess."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(answer_path), *extra],
        capture_output=True, text=True, cwd=REPO)


def load_fixture(name):
    with open(FIXTURES / name) as fh:
        return json.load(fh)


def write_answer(dir_path, data, name="answer.json"):
    """Write an answer dict to dir_path/name; returns the path."""
    p = Path(dir_path) / name
    p.write_text(json.dumps(data, indent=2))
    return p


VALID = {
    "answer": "Yes - PD-decider wins on throughput at equal p99.",
    "deliverable": "deliverable.md",
    "repro": "python3 scripts/run_study.py --question q1 --seeds 3",
    "claims": [
        {"id": "c1", "metric": "throughput_rps_mean", "value": 4100.5,
         "condition": "BLIS sim, 3 seeds", "evidence": "evidence-results.json"},
        {"id": "c2", "metric": "winning_policy", "value": "PD-decider",
         "condition": "BLIS sim, aggregate", "evidence": "evidence-notes.txt"},
    ],
}


class CliContractTests(unittest.TestCase):
    """Subprocess runs of the CLI against the fixtures."""

    def test_valid_answer(self):
        r = run_validator(FIXTURES / "answer-valid.json", "--root", FIXTURES)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")  # single-command repro: no warnings
        self.assertIn("c1 OK throughput_rps_mean=4100.5 "
                      f"evidence={FIXTURES / 'evidence-results.json'}", r.stdout)
        self.assertIn("c2 OK winning_policy=\"PD-decider\"", r.stdout)
        self.assertIn("c3 OK iterations_mode=12", r.stdout)
        self.assertIn("OK: 3 claims verified", r.stdout)

    def test_missing_key(self):
        r = run_validator(FIXTURES / "answer-missing-key.json", "--root", FIXTURES)
        self.assertEqual(r.returncode, 1)
        self.assertIn('top level: missing key "repro"', r.stderr)

    def test_extra_key(self):
        r = run_validator(FIXTURES / "answer-extra-key.json", "--root", FIXTURES)
        self.assertEqual(r.returncode, 1)
        self.assertIn('top level: unexpected key "confidence"', r.stderr)

    def test_empty_claims(self):
        r = run_validator(FIXTURES / "answer-empty-claims.json", "--root", FIXTURES)
        self.assertEqual(r.returncode, 1)
        self.assertIn("claims: must be a non-empty list", r.stderr)

    def test_duplicate_ids(self):
        r = run_validator(FIXTURES / "answer-duplicate-ids.json", "--root", FIXTURES)
        self.assertEqual(r.returncode, 1)
        self.assertIn('claims: duplicate id "c1"', r.stderr)

    def test_missing_evidence_file(self):
        r = run_validator(FIXTURES / "answer-missing-evidence.json", "--root", FIXTURES)
        self.assertEqual(r.returncode, 1)
        self.assertIn("c1: evidence file does not exist", r.stderr)

    def test_evidence_file_lacks_value(self):
        # THE key negative test: the file exists but the number is not in it.
        r = run_validator(FIXTURES / "answer-evidence-lacks-value.json", "--root", FIXTURES)
        self.assertEqual(r.returncode, 1)
        self.assertIn("c1: evidence file does not contain value 9999.9", r.stderr)

    def test_missing_deliverable(self):
        r = run_validator(FIXTURES / "answer-missing-deliverable.json", "--root", FIXTURES)
        self.assertEqual(r.returncode, 1)
        self.assertIn("deliverable: path does not exist", r.stderr)

    def test_malformed_json(self):
        r = run_validator(FIXTURES / "answer-malformed.json", "--root", FIXTURES)
        self.assertEqual(r.returncode, 2)
        self.assertIn("not valid JSON", r.stderr)

    def test_missing_argument_is_usage_error(self):
        r = subprocess.run([sys.executable, str(SCRIPT)],
                           capture_output=True, text=True, cwd=REPO)
        self.assertEqual(r.returncode, 2)

    def test_missing_answer_file(self):
        r = run_validator(FIXTURES / "answer-nope.json")
        self.assertEqual(r.returncode, 2)
        self.assertIn("not found", r.stderr)

    def test_relative_evidence_resolves_against_root(self):
        # Same valid fixture, wrong root: the relative evidence paths must
        # miss, proving they resolve against --root and not the fixtures dir.
        with tempfile.TemporaryDirectory() as td:
            r = run_validator(FIXTURES / "answer-valid.json", "--root", td)
            self.assertEqual(r.returncode, 1)
            self.assertIn("c1: evidence file does not exist", r.stderr)

    def test_default_root_is_study_dir(self):
        # investigations/<slug>/final/answer.json must resolve evidence
        # against investigations/<slug>/ with no --root passed.
        with tempfile.TemporaryDirectory() as td:
            study = Path(td) / "studies" / "q1-slug"
            (study / "final").mkdir(parents=True)
            (study / "evidence.json").write_text('{"score": 88}\n')
            answer = dict(VALID, deliverable=None, claims=[
                {"id": "c1", "metric": "score", "value": 88,
                 "condition": "fixture", "evidence": "evidence.json"}])
            p = write_answer(study / "final", answer)
            r = run_validator(p)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("OK: 1 claims verified", r.stdout)

    def test_repro_semicolon_warns_but_passes(self):
        with tempfile.TemporaryDirectory() as td:
            answer = dict(VALID, repro="make data; python3 analyze.py")
            p = write_answer(td, answer)
            r = run_validator(p, "--root", FIXTURES)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("warning:", r.stderr)
            self.assertIn("ONE command", r.stderr)
            self.assertIn("OK: 2 claims verified", r.stdout)

    def test_bool_value_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            answer = dict(VALID)
            answer["claims"] = [dict(VALID["claims"][0], value=True)]
            p = write_answer(td, answer)
            r = run_validator(p, "--root", FIXTURES)
            self.assertEqual(r.returncode, 1)
            self.assertIn("bool", r.stderr)

    def test_fabricated_number_in_unrelated_log_fails(self):
        # End-to-end iron-rule attack from the hive's security finding: two
        # entirely fabricated claims ({value: 2}, {value: 11}) citing an
        # ordinary log file passed, because "2" matched inside "2026" and
        # "11" inside the date "2026-11-10". They must now fail.
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "evidence.log").write_text(
                "Investigation started 2026-11-10\n"
                "router deployment: 3 replicas running, pid 4242\n"
                "sha256:abcdef123456\n")
            answer = dict(VALID, deliverable=None, claims=[
                {"id": "c1", "metric": "p99_latency_ms", "value": 2,
                 "condition": "fabricated", "evidence": "evidence.log"},
                {"id": "c2", "metric": "error_rate_pct", "value": 11,
                 "condition": "fabricated", "evidence": "evidence.log"}])
            p = write_answer(td, answer)
            r = run_validator(p, "--root", td)
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("c1: evidence file does not contain value 2", r.stderr)
            self.assertIn("c2: evidence file does not contain value 11", r.stderr)

    def test_standalone_number_in_log_passes(self):
        # The positive control for the same evidence text: a claim whose
        # value really is in the log (3 replicas, pid 4242) still verifies.
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "evidence.log").write_text(
                "Investigation started 2026-11-10\n"
                "router deployment: 3 replicas running, pid 4242\n")
            answer = dict(VALID, deliverable=None, claims=[
                {"id": "c1", "metric": "replicas", "value": 3,
                 "condition": "control", "evidence": "evidence.log"},
                {"id": "c2", "metric": "router_pid", "value": 4242,
                 "condition": "control", "evidence": "evidence.log"}])
            p = write_answer(td, answer)
            r = run_validator(p, "--root", td)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("OK: 2 claims verified", r.stdout)


class IronRuleUnitTests(unittest.TestCase):
    """Direct tests of the contains-value helpers."""

    def test_numeric_forms(self):
        forms = va.numeric_forms(4100.0)
        self.assertIn("4100.0", forms)   # repr
        self.assertIn("4100", forms)     # trimmed trailing zeros
        self.assertIn("4100.00", forms)  # padded fixed decimals
        forms = va.numeric_forms(7)
        self.assertIn("7", forms)
        self.assertIn("7.0", forms)
        self.assertEqual(va.numeric_forms(True), [])  # bools are not numbers

    def test_numeric_forms_never_round(self):
        # A rounded spelling would let 0.347 match a bare "0.3" in the text.
        self.assertNotIn("0.3", va.numeric_forms(0.347))
        self.assertNotIn("0.35", va.numeric_forms(0.347))

    def test_text_contains(self):
        self.assertTrue(va.text_contains("mean 4100.5 rps", 4100.5))
        self.assertTrue(va.text_contains("mean 4100 rps", 4100.0))  # trimmed
        self.assertFalse(va.text_contains("no numbers here", 4100.5))
        self.assertTrue(va.text_contains("policy PD-decider wins", "PD-decider"))
        self.assertFalse(va.text_contains("policy wins", "PD-decider"))

    def test_text_contains_anchors_numbers(self):
        # THE adversarial case the substring match missed: any ordinary log
        # text (dates, pids, ports, sha fragments) satisfied a fabricated
        # small-number claim. A number now matches only as a standalone
        # number — and '-' is in the anchor class exactly because of dates.
        log = ("Investigation started 2026-11-10 / router deployment: "
               "3 replicas running, pid 4242 / sha256:abcdef123456")
        for fabricated in (2, 10, 11, 26, 42, 2026):
            self.assertFalse(va.text_contains(log, fabricated),
                             f"{fabricated} matched inside ordinary log text")
        self.assertTrue(va.text_contains(log, 3))   # "3 replicas" is real
        self.assertTrue(va.text_contains(log, 4242))  # standalone pid
        # Version/build strings and decimal fractions are not numbers either.
        self.assertFalse(va.text_contains("v1.5.2", 5))
        self.assertFalse(va.text_contains("latency was 50ms", 5))
        self.assertFalse(va.text_contains("7.5 ops", 7))

    def test_text_contains_keeps_legitimate_spellings(self):
        # Everything a real analysis script prints still matches.
        self.assertTrue(va.text_contains("mean 4100.5 rps", 4100.5))
        self.assertTrue(va.text_contains("rate 0.347 per-op", 0.347))
        self.assertTrue(va.text_contains("p99=10ms", 10))       # unit suffix
        self.assertTrue(va.text_contains("took 10s", 10))
        self.assertTrue(va.text_contains("10 replicas", 10))
        self.assertTrue(va.text_contains("count: 7.", 7))       # sentence end
        self.assertTrue(va.text_contains("settled at 4100.", 4100))
        self.assertTrue(va.text_contains("delta: -5", -5))      # whole form
        self.assertTrue(va.text_contains("(42)", 42))           # punctuation
        # Deliberate tightening, documented in the fix: a range endpoint
        # is an interval the value falls inside, not the measured value.
        self.assertFalse(va.text_contains("values 0-100", 100))
        self.assertFalse(va.text_contains("1.2.3", 2))          # dotted triple

    def test_large_file_scan_anchors_numbers(self):
        # The line-by-line path (files over LARGE_FILE_BYTES) must anchor
        # the same way: a fabricated value inside a date line is not found.
        old = va.LARGE_FILE_BYTES
        va.LARGE_FILE_BYTES = 64
        try:
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "big.txt"
                p.write_text("filler line one\nfiller line two\n"
                             "started 2026-11-10 pid 4242\n"
                             "filler line five\n")
                self.assertFalse(va.file_contains(p, 10)[0])
                self.assertFalse(va.file_contains(p, 11)[0])
                self.assertTrue(va.file_contains(p, 4242)[0])
                # String claims keep their plain-substring semantics on the
                # same line-by-line path.
                q = Path(td) / "big-str.txt"
                q.write_text("filler line one\nfiller line two\n"
                             "winner PD-decider at p99 parity\n"
                             "filler line five\n")
                self.assertTrue(va.file_contains(q, "PD-decider")[0])
                self.assertFalse(va.file_contains(q, "threshold-static")[0])
        finally:
            va.LARGE_FILE_BYTES = old

    def test_json_structural_match_nested(self):
        found, err = va.file_contains(FIXTURES / "evidence-results.json", 4100.5)
        self.assertIsNone(err)
        self.assertTrue(found)  # aggregate.throughput_rps_mean, nested

    def test_numeric_tolerance_matches_nous(self):
        # rel_tol=1e-3: 100 vs 100.05 (0.05%) matches, 100 vs 101 (1%) does not.
        with tempfile.TemporaryDirectory() as td:
            close = Path(td) / "close.json"
            close.write_text('{"n": 100.05}')
            far = Path(td) / "far.json"
            far.write_text('{"n": 101}')
            self.assertTrue(va.file_contains(close, 100)[0])
            self.assertFalse(va.file_contains(far, 100)[0])

    def test_int_in_json_satisfies_float_claim(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "int.json"
            p.write_text('{"n": 4100}')
            self.assertTrue(va.file_contains(p, 4100.0)[0])

    def test_structural_only_match_via_scientific_notation(self):
        # "1e6" in the raw text matches no substring spelling of 1000000.0;
        # only the recursive numeric scan can find it.
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sci.json"
            p.write_text('{"n": 1e6}')
            self.assertTrue(va.file_contains(p, 1000000.0)[0])

    def test_bool_scalar_is_not_a_number(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "flag.json"
            p.write_text('{"flag": true}')
            self.assertFalse(va.file_contains(p, 1)[0])
            self.assertFalse(va.file_contains(p, 0)[0])

    def test_string_structural_match(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "nested.json"
            p.write_text('{"a": {"b": "PD-decider"}}')
            self.assertTrue(va.file_contains(p, "PD-decider")[0])
            self.assertFalse(va.file_contains(p, "threshold-static")[0])

    def test_large_file_scanned_line_by_line(self):
        # Shrink the threshold so a small file takes the large-file path:
        # it must still find values without ever loading the file whole.
        old = va.LARGE_FILE_BYTES
        va.LARGE_FILE_BYTES = 64
        try:
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "big.txt"
                p.write_text("filler line one\nfiller line two\nfiller line three\n"
                             "score 4100.5 rps\nfiller line five\n")
                self.assertGreater(p.stat().st_size, va.LARGE_FILE_BYTES)
                self.assertTrue(va.file_contains(p, 4100.5)[0])
                self.assertFalse(va.file_contains(p, 9999.9)[0])
        finally:
            va.LARGE_FILE_BYTES = old


class ClaimShapeUnitTests(unittest.TestCase):
    """Direct tests of validate_answer's claim-shape errors."""

    def check(self, claims):
        data = dict(VALID, claims=claims)
        errors, _, _ = va.validate_answer(data, Path("/nonexistent-root"))
        return errors

    def test_claim_extra_key(self):
        errors = self.check([dict(VALID["claims"][0], note="extra")])
        self.assertTrue(any('unexpected key "note"' in e for e in errors))

    def test_claim_missing_key(self):
        claim = {k: v for k, v in VALID["claims"][0].items() if k != "condition"}
        errors = self.check([claim])
        self.assertTrue(any('missing key "condition"' in e for e in errors))

    def test_claim_empty_metric(self):
        errors = self.check([dict(VALID["claims"][0], metric="")])
        self.assertTrue(any("metric must be a non-empty string" in e for e in errors))

    def test_claims_not_a_list(self):
        errors = self.check({"c1": "not a list"})
        self.assertTrue(any("claims: must be a list of claim objects" in e
                            for e in errors))


if __name__ == "__main__":
    unittest.main()
