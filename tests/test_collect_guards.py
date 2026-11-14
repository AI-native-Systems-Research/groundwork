"""Tests for collect.py's guard and skip branches, in-process.

tests/test_collect.py drives the script as a subprocess against fixtures, so
the happy paths are covered but the branches that decide between "record a
guard failure and keep collecting" and "refuse the work dir" were dead, as
were the per-row skips in hypotheses()/limitations()/methods(). These tests
import collect directly (the house in-process pattern of
test_doctor_inprocess.py) and build minimal synthetic work dirs in a
tempdir, so every branch counts without a subprocess.

Every test here pins behaviour a regression would silently break: a work dir
whose campaign.yaml is unreadable must still produce a bundle with the guard
carrying the error, not a traceback; a findings.json row that is not an
object must be skipped, not crash the whole collection.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXAMPLE = FIXTURES / "workdir-example"
SCRIPT = REPO / "skills/investigation/scripts" / "collect.py"

sys.path.insert(0, str(REPO / "skills/investigation/scripts"))
import collect  # noqa: E402


def make_work_dir(td: Path, **files) -> Path:
    """A minimal v2 work dir: state.json finished, plus any named files."""
    d = td / "w"
    d.mkdir()
    (d / "state.json").write_text(json.dumps(
        {"phase": "REPORT", "iteration": 1, "stop_reason": "answered"}))
    for name, content in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, Path):
            content = content.read_text()
        p.write_text(content)
    return d


class LoadersTest(unittest.TestCase):
    """load_json / load_yaml: unreadable is a guard error, not a crash."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.d = Path(self._td.name)

    def test_load_json_unreadable_directory(self):
        # read_text() on a directory raises IsADirectoryError (an OSError):
        # the artifact is present but unreadable, which is a guard detail.
        (self.d / "a.json").mkdir()
        data, err = collect.load_json(self.d / "a.json")
        self.assertIsNone(data)
        self.assertIn("a.json is unreadable", err)

    def test_load_yaml_unreadable_directory(self):
        (self.d / "c.yaml").mkdir()
        data, err = collect.load_yaml(self.d / "c.yaml")
        self.assertIsNone(data)
        self.assertIn("c.yaml is unreadable", err)

    def test_load_json_unreadable_permission(self):
        if os.geteuid() == 0:  # root ignores the mode bits; directory case above
            self.skipTest("running as root: permission bits not enforced")
        p = self.d / "b.json"
        p.write_text("{}")
        p.chmod(0)
        try:
            data, err = collect.load_json(p)
        finally:
            p.chmod(stat.S_IRUSR | stat.S_IWUSR)
        self.assertIsNone(data)
        self.assertIn("b.json is unreadable", err)


class ClaimVerifiedTiersTest(unittest.TestCase):
    """The remaining attribution tiers in claim_verified() and its helpers."""

    def test_evidence_mapping_path_is_used_for_file_taint(self):
        # _claim_evidence_path: a {path} evidence mapping and a
        # {path} evidence_detail both name the file the bare-name tier
        # matches on.
        err = "evidence path does not exist: runs/R4.json (searched work dir)"
        for claim in ({"id": "C1", "metric": "m", "evidence": {"path": "runs/R4.json"}},
                      {"id": "C2", "metric": "m", "evidence_detail": {"path": "runs/R4.json"}}):
            self.assertFalse(collect.claim_verified(claim, [err]), claim)
        # a different file's claim is untouched
        self.assertTrue(collect.claim_verified(
            {"id": "C3", "metric": "m", "evidence": {"path": "runs/R9.json"}}, [err]))

    def test_names_file_requires_an_extension(self):
        # A basename with no '.' cannot be named by the bare-file tier: the
        # claim's evidence path "notes" never matches prose mentioning notes.
        self.assertFalse(collect._names_file("see notes for detail", "notes"))
        self.assertTrue(collect._names_file("see r4.json for detail", "runs/r4.json"))

    def test_attributable_by_id_pointer_and_file(self):
        self.assertTrue(collect._attributable("claim C7: boom"))
        self.assertTrue(collect._attributable("mismatch at r4.json#/p99_ms"))
        self.assertFalse(collect._attributable("nothing to see here"))

    def test_non_string_evidence_errors_are_skipped(self):
        # final_check.evidence_errors may hold junk rows; a non-string entry
        # is skipped, not crashed on, and taints nobody on its own.
        errors = [42, None, "claim C1: value mismatch"]
        self.assertTrue(collect.claim_verified({"id": "C2", "metric": "m"}, errors))
        self.assertFalse(collect.claim_verified({"id": "C1", "metric": "m"}, errors))


class BuildBundleGuardsTest(unittest.TestCase):
    """build_bundle's guard outcomes for degraded-but-collectable work dirs."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.td = Path(self._td.name)

    def guards(self, wd):
        b = collect.build_bundle(wd)
        return b, {g["label"]: g for g in b["guards"]}

    def test_state_json_not_an_object_is_refused(self):
        wd = make_work_dir(self.td)
        (wd / "state.json").write_text("[1, 2]")
        with self.assertRaises(collect.CouldNotRun) as cm:
            collect.build_bundle(wd)
        self.assertIn("not a JSON object", str(cm.exception))

    def test_state_json_without_phase_key_is_refused(self):
        wd = make_work_dir(self.td)
        (wd / "state.json").write_text('{"session_id": "s"}')
        with self.assertRaises(collect.CouldNotRun) as cm:
            collect.build_bundle(wd)
        self.assertIn("has no 'phase' key", str(cm.exception))

    def test_unreadable_campaign_yaml_is_a_guard_not_a_refusal(self):
        wd = make_work_dir(self.td)
        (wd / "campaign.yaml").mkdir()  # read_text -> IsADirectoryError
        b, g = self.guards(wd)
        self.assertFalse(b["usable"])
        self.assertFalse(g["objective declared"]["ok"])
        self.assertIn("campaign.yaml is unreadable", g["objective declared"]["detail"])

    def test_absent_campaign_yaml_is_a_guard(self):
        wd = make_work_dir(self.td)
        _, g = self.guards(wd)
        self.assertIn("campaign.yaml is absent", g["objective declared"]["detail"])

    def test_non_mapping_campaign_yaml_is_a_guard(self):
        wd = make_work_dir(self.td, **{"campaign.yaml": "- just\n- a list\n"})
        _, g = self.guards(wd)
        self.assertIn("campaign.yaml is not a mapping", g["objective declared"]["detail"])

    def test_campaign_without_objective_is_a_guard(self):
        wd = make_work_dir(self.td, **{"campaign.yaml": "run_id: r\n"})
        _, g = self.guards(wd)
        self.assertFalse(g["objective declared"]["ok"])
        self.assertIn("declares no objective.metric + objective.direction",
                      g["objective declared"]["detail"])

    def test_unreadable_final_check_is_a_guard_and_answer_stays_none(self):
        wd = make_work_dir(self.td, **{"campaign.yaml": "objective: {metric: m, direction: increase}\n"})
        (wd / "final_check.json").mkdir()
        b, g = self.guards(wd)
        self.assertFalse(g["answer checked"]["ok"])
        self.assertIn("final_check.json is unreadable", g["answer checked"]["detail"])
        self.assertIsNone(b["final_check"])  # 384-385: not just unverified

    def test_non_object_final_check_is_a_guard_and_answer_stays_none(self):
        wd = make_work_dir(self.td,
                           **{"campaign.yaml": "objective: {metric: m, direction: increase}\n",
                              "final_check.json": "[1]"})
        b, g = self.guards(wd)
        self.assertIn("final_check.json is not a JSON object", g["answer checked"]["detail"])
        self.assertIsNone(b["final_check"])  # 389-390

    def test_unreadable_baseline_is_a_guard(self):
        wd = make_work_dir(self.td, **{"campaign.yaml": "objective: {metric: m, direction: increase}\n"})
        (wd / "baseline.json").mkdir()
        _, g = self.guards(wd)
        self.assertIn("baseline.json is unreadable", g["baseline"]["detail"])

    def test_non_object_baseline_is_a_guard(self):
        wd = make_work_dir(self.td,
                           **{"campaign.yaml": "objective: {metric: m, direction: increase}\n",
                              "baseline.json": '"just a string"'})
        _, g = self.guards(wd)
        self.assertIn("baseline.json is not a JSON object", g["baseline"]["detail"])


class PerRowSkipsTest(unittest.TestCase):
    """hypotheses()/limitations()/methods(): junk rows are skipped, real
    signals still produce their entries."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.td = Path(self._td.name)
        self.wd = make_work_dir(
            self.td,
            **{
                "campaign.yaml": "objective: {metric: m, direction: increase}\n",
                "final_check.json": json.dumps(
                    {"answer_valid": True, "evidence_errors": []}),
                "runs/iter-1/findings.json": json.dumps({
                    "hypotheses": [
                        {"id": "H1", "status": "CONFIRMED"},   # statement from bundle
                        "not-a-mapping",                       # 515: skipped
                    ],
                    "reason": "iter reason",
                }),
                "runs/iter-1/bundle.json": json.dumps({
                    "hypotheses": [
                        {"id": "H1", "role": "main", "mechanism": "mech"},  # real
                        "junk-row",                                 # 519: skipped
                    ]}),
            })

    def test_hypotheses_skips_non_mapping_rows(self):
        out = collect.hypotheses(self.wd)
        self.assertEqual([h["id"] for h in out], ["H1"])
        self.assertEqual(out[0]["statement"]["role"], "main")

    def test_non_object_findings_skip_the_whole_iteration(self):
        # findings.json as a JSON list (or with hypotheses not a list): the
        # iteration dir contributes nothing, and a later good iteration
        # still does — the skip is per-dir, not fatal.
        (self.wd / "runs/iter-1/findings.json").write_text("[1, 2]")
        (self.wd / "runs/iter-2").mkdir()
        (self.wd / "runs/iter-2/findings.json").write_text(json.dumps(
            {"hypotheses": [{"id": "H9", "status": "REFUTED"}]}))
        out = collect.hypotheses(self.wd)
        self.assertEqual([(h["iteration"], h["id"]) for h in out], [(2, "H9")])
        # limitations() skips the same broken dir the same way
        lims = collect.limitations(self.wd)
        self.assertEqual(lims, [])

    def test_hypotheses_without_any_bundle_returns_no_statements(self):
        (self.wd / "runs/iter-1/bundle.json").unlink()
        out = collect.hypotheses(self.wd)
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0]["statement"])      # 505: {} + None path
        self.assertIsNone(out[0]["statement_ref"])

    def test_hypothesis_without_a_string_id(self):
        (self.wd / "runs/iter-1/findings.json").write_text(json.dumps(
            {"hypotheses": [{"status": "CONFIRMED"}, {"id": 7, "status": "REFUTED"}]}))
        out = collect.hypotheses(self.wd)
        self.assertEqual([h["id"] for h in out], [None, 7])
        self.assertTrue(all(h["statement"] is None for h in out))

    def test_limitations_skip_junk_rows_and_record_the_real_signals(self):
        (self.wd / "ledger.json").write_text(json.dumps([
            "junk-row",                                     # 582: skipped
            {"iteration": 2, "status": "partial"},
            {"iteration": 3, "prereg_violation": True},     # 587
        ]))
        (self.wd / "baseline.json").write_text(json.dumps(
            {"noise": {"unmeasured": True, "reason": "one seed"}}))   # 592
        (self.wd / "runs/iter-1/findings.json").write_text(json.dumps({
            "hypotheses": ["junk-row",                       # 551: skipped
                           {"id": "H1", "noise_unmeasured": True, "noise_reason": "one seed"}],
            "reason": "why",
        }))
        lims = collect.limitations(self.wd)
        srcs = [x["source"] for x in lims]
        self.assertIn("nous:findings.reason", srcs)
        self.assertIn("nous:findings.noise_unmeasured", srcs)
        self.assertIn("nous:ledger.status", srcs)
        self.assertIn("nous:ledger.prereg_violation", srcs)
        self.assertIn("nous:baseline.noise.unmeasured", srcs)
        prereg = next(x for x in lims if x["source"] == "nous:ledger.prereg_violation")
        self.assertIn("iteration 3", prereg["text"])
        self.assertIn("changed after its predictions were locked", prereg["text"])
        noise = next(x for x in lims if x["source"] == "nous:baseline.noise.unmeasured")
        self.assertIn("one seed", noise["text"])

    def test_methods_skips_unparseable_runs_jsonl_rows(self):
        (self.wd / "runs.jsonl").write_text("\n".join([
            "not json at all",                              # 622-623: skipped
            json.dumps({"experiment": True, "phase": "EXECUTE", "iter": 1,
                        "command": "c", "exit_code": 0}),
            json.dumps({"experiment": False}),              # not an experiment
        ]) + "\n")
        m = collect.methods(self.wd)
        self.assertEqual(len(m["experiment_runs"]), 1)
        self.assertEqual(m["experiment_runs"][0]["command"], "c")
        self.assertTrue(m["experiment_runs_ref"].endswith("runs.jsonl"))


class MainStudyArgTest(unittest.TestCase):
    """main(): --study pointing at a missing dir is exit 1, not a traceback."""

    def test_study_not_a_directory(self):
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run(
                [sys.executable, str(SCRIPT), str(EXAMPLE), "--study", str(Path(td) / "nope")],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 1)
            self.assertIn("--study", r.stderr)
            self.assertIn("is not a directory", r.stderr)
            self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
