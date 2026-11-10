"""R9: Milestone A acceptance matrix (spec §14; plan §5 R9).

Every spec §14 acceptance bullet maps to named tests. The matrix test checks
that each named test exists (dotted name, importable), so a rename or
deletion breaks acceptance visibly. Gaps the earlier phases left are filled
by the small end-to-end tests below (fake adapter / R4 fixtures; nothing
real is launched).

Status per row:
  "pass"            covered by the named mocked tests (and, where the row
                    says "real adapter", pending its R9 real run)
  "open-D10"        the telemetry-driven path is ON HOLD (owner decision
                    D10: R6a/R6 not built); reported as a SKIPPED test,
                    never as a pass
  "real-run"        mocked tests cannot establish native compatibility
                    (spec §14); the real paid run needs the owner's own
                    approval and is tracked in examples/runtime-milestone-a/
"""
import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _runtime_fake_adapter as fake  # noqa: E402
from _runtime_util import REPO  # noqa: E402
from ains_runtime import adapters as A  # noqa: E402
from ains_runtime import approval as AP  # noqa: E402
from ains_runtime import contracts  # noqa: E402
from ains_runtime import preparation as P  # noqa: E402
from ains_runtime import supervisor as SV  # noqa: E402
import test_runtime_prepare as R2  # noqa: E402
import test_runtime_results as R7  # noqa: E402

D10 = "D10: R6a/R6 on hold (Spotlights telemetry-driven adapter not built)"
EXAMPLE = REPO / "examples" / "runtime-milestone-a"

# (spec §14 bullet, status, [tests by dotted name], note)
MATRIX = [
    ("Nous / Spotlights static: a valid approved preparation launches through the "
     "real adapter; a material mutation is rejected", "pass+real-run", [
        "test_runtime_nous.MaterialMutation.test_each_material_field_mutation_is_rejected_after_approval",
        "test_runtime_nous.MaterialMutation.test_every_material_field_is_covered",
        "test_runtime_nous.MaterialMutation.test_apparatus_change_after_approval_blocks_launch",
        "test_runtime_nous.MaterialMutation.test_engine_upgrade_after_approval_blocks_launch",
        "test_runtime_spotlights_static.Prepare.test_material_mutation_rejected_after_approval",
        "test_runtime_spotlights_static.Prepare.test_each_material_field_changes_the_digest",
        "test_runtime_spotlights_static.Exec.test_launch_classify_collect_end_to_end",
        "test_runtime_exec.Launch.test_succeed_end_to_end",
        "test_runtime_exec.Launch.test_drift_after_approval_blocks_launch",
        "test_runtime_prepare.Revalidate.test_material_change_after_approval_leaves_old_approval_unusable",
    ], "real Nous + Spotlights static runs: examples/runtime-milestone-a/ (owner approval)"),
    ("Spotlights telemetry-driven: approved preparation launches through the real "
     "adapter; material mutation rejected", "open-D10", [], D10),
    ("Spotlights static preserves native artifacts, collects evidence and candidate "
     "references, distinguishes no-opportunity from failure, never reports estimated "
     "impact as a measured claim", "pass", [
        "test_runtime_spotlights_static.Collect.test_candidates_found_collects_citations",
        "test_runtime_spotlights_static.Collect.test_collect_is_read_only_over_native_output",
        "test_runtime_spotlights_static.Collect.test_estimated_impact_never_a_claim_value",
        "test_runtime_spotlights_static.Collect.test_guard_rejects_an_estimate_claim",
        "test_runtime_spotlights_static.Classify.test_no_opportunity_vs_failed",
        "test_runtime_spotlights_static.Classify.test_failed_rules",
        "test_runtime_results.SpotlightsResults.test_no_opportunity_is_a_completed_execution",
        "test_runtime_results.SpotlightsResults.test_failed_run_has_no_findings",
        "test_runtime_results.SpotlightsResults.test_results_guard_catches_an_estimate_the_adapter_missed",
        "test_runtime_results.SpotlightsResults.test_candidates_found_is_other_and_estimates_never_become_claims",
    ], ""),
    ("Spotlights telemetry-driven preserves artifacts, distinguishes no-opportunity "
     "from failure, no estimate as claim", "open-D10", [], D10),
    ("A Spotlights result can lead to stopping, clarification, or a separately "
     "justified and approved Nous request; no automatic chaining", "pass", [
        "test_runtime_docs.SpotlightsResultLaunchesNothing.test_candidates_result_creates_nothing_next",
        "test_runtime_docs.NoAutoChainingDocs.test_docs_say_stop_clarify_or_a_separately_approved_request",
        "test_runtime_docs.NoAutoChainingCode.test_result_paths_never_call_launch_reserve_prepare_or_submit",
        "test_runtime_docs.NoAutoChainingCode.test_cli_collect_and_result_handlers_never_launch",
        "test_runtime_exec.Launch.test_supervisor_never_chains_or_classifies",
        "test_runtime_acceptance.SpotlightsToNous.test_follow_on_nous_request_needs_justification_and_its_own_go",
    ], ""),
    ("The telemetry-driven Spotlights path rejects missing or incompatible inputs and "
     "cannot execute signal-pipeline stage 05", "open-D10", [
        "test_runtime_docs.NoStage05.test_stage_05_is_never_instructed",
        "test_runtime_docs.NoStage05.test_signal_pipeline_command_stops_at_04",
    ], D10 + "; until then the legacy gate's docs never instruct stage 05 "
             "(the listed doc tests pass), but no adapter enforces it"),
    ("Duplicate delivery and concurrent equivalent submissions do not start duplicate "
     "jobs", "pass", [
        "test_runtime_store.Submit.test_same_id_same_payload_returns_existing_receipt",
        "test_runtime_store.Submit.test_same_id_different_payload_is_conflict",
        "test_runtime_store.Submit.test_concurrent_identical_submissions_store_once",
        "test_runtime_store.Grants.test_equivalent_material_blocked_by_partial_unique_index",
        "test_runtime_exec.Concurrency.test_concurrent_equivalent_launches_start_exactly_one_engine",
        "test_runtime_exec.Concurrency.test_reservation_failure_rolls_back_consumption",
        "test_runtime_cli.Submit.test_duplicate_and_conflict_exit_codes",
        "test_runtime_prepare.Prepare.test_equivalent_requests_get_the_same_digest",
    ], ""),
    ("Crashes before and after spawn recover known state or stop for reconciliation "
     "without automatic relaunch", "pass", [
        "test_runtime_exec.Crash.test_crash_between_reserve_and_spawn_needs_owner_reconcile",
        "test_runtime_exec.Crash.test_supervisor_crash_before_spawn",
        "test_runtime_exec.Crash.test_supervisor_crash_after_spawn_keeps_evidence",
        "test_runtime_exec.Crash.test_supervisor_crash_after_running_reconciles_to_real_state",
        "test_runtime_exec.Crash.test_supervisor_crash_after_exit_classifies",
        "test_runtime_exec.Crash.test_reconcile_refuses_non_recovery_and_secret_quote",
        "test_runtime_exec.ProcId.test_pid_reused_by_a_different_process_is_not_the_engine",
    ], ""),
    ("Missing requirements and unavailable/version-mismatched capabilities fail "
     "before launch", "pass", [
        "test_runtime_nous.Validate.test_required_semantics",
        "test_runtime_nous.Validate.test_availability_and_pin",
        "test_runtime_nous.Validate.test_apparatus_locks",
        "test_runtime_nous.Validate.test_non_blis_questions_rejected_with_reason",
        "test_runtime_spotlights_static.Prepare.test_required_semantics",
        "test_runtime_spotlights_static.Prepare.test_engine_sha_must_equal_pin",
        "test_runtime_spotlights_static.Prepare.test_include_must_exist_at_the_commit",
        "test_runtime_prepare.Prepare.test_unknown_work_and_unknown_capability_refused",
        "test_runtime_prepare.Revalidate.test_engine_version_drift",
        "test_runtime_prepare.Revalidate.test_adapter_missing_at_launch",
        "test_runtime_contracts.MessageTypes.test_required_fields_never_inferred",
        "test_runtime_exec.ProcId.test_missing_ps_and_proc_blocks_launch",
    ], ""),
    ("Cancellation and partial execution are represented truthfully", "pass", [
        "test_runtime_exec.Cancel.test_cancel_while_running",
        "test_runtime_exec.Cancel.test_cancel_after_engine_finished_reports_real_state",
        "test_runtime_exec.Cancel.test_cancel_receipt_is_not_a_stop",
        "test_runtime_exec.Cancel.test_cancel_before_spawn",
        "test_runtime_exec.Cancel.test_survivors_keep_cancel_unconfirmed",
        "test_runtime_nous.Classify.test_stop_sentinel_with_cancel",
        "test_runtime_nous.Classify.test_sigint_after_cancel_is_cancelled",
        "test_runtime_spotlights_static.Classify.test_cancelled_without_result",
        "test_runtime_spotlights_static.Classify.test_cancel_requested_but_finished_reports_real_state",
        "test_runtime_results.NousResults.test_halt_is_failed_with_partial_findings",
        "test_runtime_results.SpotlightsResults.test_partial_is_labelled",
    ], ""),
    ("Negative and inconclusive findings remain successful executions when "
     "justified", "pass", [
        "test_runtime_results.NousResults.test_inconclusive_and_negative_findings_are_completed_executions",
        "test_runtime_results.NousResults.test_coordinator_negative_and_inconclusive_outcomes_stay_completed",
        "test_runtime_results.NousResults.test_coordinator_outcome_refused_without_hypothesis_or_completion",
        "test_runtime_results.SpotlightsResults.test_no_opportunity_is_a_completed_execution",
    ], ""),
    ("Collection and validation failures preserve native evidence and do not rerun "
     "the experiment", "pass", [
        "test_runtime_results.NousResults.test_collection_failure_preserves_evidence_and_never_reruns",
        "test_runtime_results.NousResults.test_validation_failure_preserves_evidence_and_never_reruns",
        "test_runtime_results.SpotlightsResults.test_adapter_guard_failure_is_a_collection_failure",
        "test_runtime_nous.Classify.test_collect_is_read_only",
    ], ""),
    ("A next step requires fresh justification and approval; stopping requires no "
     "follow-on launch", "pass", [
        "test_runtime_acceptance.NextStep.test_request_after_a_result_needs_next_step",
        "test_runtime_acceptance.NextStep.test_first_request_needs_none",
        "test_runtime_acceptance.NextStep.test_justification_is_on_the_card",
        "test_runtime_acceptance.NextStep.test_stopping_needs_no_launch",
        "test_runtime_prepare.Approve.test_single_use_and_bound_to_one_digest",
        "test_runtime_docs.NoAutoChainingDocs.test_plan_approval_grants_no_launch",
        "test_runtime_cli.CopiedStudyGrantsNothing.test_replayed_request_plus_reprepared_needs_fresh_approval",
    ], ""),
    ("Installation into a separate target repository works without Hive/Crux or "
     "checkout-specific paths", "pass", [
        "test_runtime_acceptance.InstallSmoke.test_install_smoke_is_wired_into_ci",
        "test_runtime_acceptance.InstallSmoke.test_shipped_entry_points_write_no_bytecode",
        "test_runtime_prepare.Cli.test_shipped_cli_has_no_fake_adapter",
        "test_runtime_cli.Isolation.test_doctor_and_validator_do_not_import_runtime",
    ], "plus the install-smoke CI job (tests/_runtime_install_smoke.py) and "
       "no-hardcoded-paths"),
    ("Existing answer validation, tracking modes, and prohibited-action rules remain "
     "intact", "pass", [
        "test_answer_contract.AnswerContractTests.test_verbatim_block_unchanged",
        "test_validate.CliContractTests.test_valid_answer",
        "test_validate_citations.CitationTests.test_valid_citation_with_repo_flag",
        "test_new_study.TestScaffold.test_fork_scaffold",
        "test_new_study.TestScaffold.test_untracked_scaffold_and_exclude_idempotent",
        "test_new_study.TestCommitGuard.test_refuses_when_push_remote_is_upstream",
        "test_runtime_docs.NoDirectLaunches.test_no_migrated_engine_command_outside_the_adapter_section",
        "test_runtime_docs.NoDirectLaunches.test_skill_md_forbids_direct_launches_and_labels_legacy",
        "test_runtime_docs.NoStage05.test_stage_05_is_never_instructed",
    ], ""),
]


def resolve(dotted: str):
    mod, cls, meth = dotted.rsplit(".", 2)
    c = getattr(importlib.import_module(mod), cls)
    return getattr(c, meth)


class Matrix(unittest.TestCase):
    def test_every_named_test_exists(self):
        for bullet, status, names, _ in MATRIX:
            for n in names:
                with self.subTest(bullet=bullet[:60], test=n):
                    self.assertTrue(callable(resolve(n)), n)

    def test_every_non_telemetry_row_has_tests(self):
        for bullet, status, names, _ in MATRIX:
            if status != "open-D10":
                with self.subTest(bullet=bullet[:60]):
                    self.assertGreaterEqual(len(names), 3, bullet)

    def test_rows_cover_every_spec_bullet(self):
        spec = (REPO / "docs" / "runtime-integration-spec.md").read_text()
        sec = spec.split("Acceptance tests must demonstrate:", 1)[1].split("Use fake ", 1)[0]
        bullets = [ln[2:].strip() for ln in sec.splitlines() if ln.startswith("- ")]
        self.assertEqual(len(bullets), 13)
        keys = ["material mutation", "never report estimated impact", "No automatic chaining",
                "cannot execute signal-pipeline stage 05", "Duplicate delivery",
                "Crashes before and after spawn", "Missing requirements",
                "Cancellation and partial", "Negative and inconclusive",
                "Collection and validation failures", "A next step requires",
                "Installation into a separate", "Existing answer validation"]
        for b, k in zip(bullets, keys):
            self.assertIn(k, b)
        rows = " | ".join(r[0] for r in MATRIX)
        for k in ("material mutation", "estimated impact", "automatic chaining", "stage 05",
                  "Duplicate delivery", "Crashes before and after spawn",
                  "Missing requirements", "Cancellation and partial",
                  "Negative and inconclusive", "Collection and validation failures",
                  "next step requires", "Installation into a separate",
                  "Existing answer validation"):
            self.assertIn(k, rows)

    def test_telemetry_rows_are_open_not_passing(self):
        open_rows = [r for r in MATRIX if r[1] == "open-D10"]
        self.assertEqual(len(open_rows), 3)
        for r in open_rows:
            self.assertIn("D10", r[3])
        # Nothing ships a telemetry adapter while D10 holds.
        self.assertNotIn("spotlights-telemetry", A.names())
        self.assertIn("spotlights-telemetry", contracts.CAPABILITIES)

    def test_real_runs_are_driven_by_plain_language_not_claimed(self):
        readme = (EXAMPLE / "README.md").read_text()
        for s in ("owner", "go", "approval card", "D10", "plain language"):
            self.assertIn(s, readme)
        # The owner never hand-writes structured input: no request files here.
        self.assertEqual(sorted(p.name for p in EXAMPLE.glob("*.json")), [])
        self.assertFalse(any(EXAMPLE.glob("*/runtime/results")),
                         "real-run results exist: update this test and the matrix")

    def test_skill_derives_structured_input(self):
        skill = (REPO / "skills" / "investigation" / "SKILL.md").read_text()
        self.assertIn("you write every structured input", skill)
        proto = (REPO / "skills" / "investigation" / "references"
                 / "runtime-protocol.md").read_text()
        self.assertIn("Where each field comes from", proto)


class TelemetryOpenUnderD10(unittest.TestCase):
    """Visible open rows: these are SKIPPED, never passing, while D10 holds."""

    @unittest.skip(D10)
    def test_telemetry_launch_through_real_adapter_and_mutation_rejected(self):
        raise AssertionError("not built")

    @unittest.skip(D10)
    def test_telemetry_artifacts_no_opportunity_vs_failure_no_estimate_claims(self):
        raise AssertionError("not built")

    @unittest.skip(D10)
    def test_telemetry_rejects_bad_inputs_and_cannot_run_stage_05(self):
        raise AssertionError("not built")


# --- gap fillers ----------------------------------------------------------------

class NextStep(R2.Base):
    """Plan §4.1: next_step is required once the study has a result; the
    justification is on the card; stopping needs no launch."""

    NS = R7.NEXT_STEP

    def finish_one(self):
        """One prepared + approved + reserved + completed invocation."""
        self.submit()
        res = self.prepare()
        self.approve(res)
        self.store.reserve_invocation("inv-done-0001", res.prep_id,
                                      launch={"argv": ["fake-engine"]})
        self.store.transition("inv-done-0001", (SV.RESERVED,), SV.COMPLETED,
                              exit_code=0, ended_at="2026-11-09T00:00:00Z")

    def follow_on(self, **over):
        sem = {"models": {"session": "model-x"}, "run_id": "followon-run-0002"}
        m = self.req("req-2", work_id="w-2", semantics=sem, **over)
        m["work_id"] = "w-2"
        self.submit(m)
        return "w-2"

    def test_first_request_needs_none(self):
        self.submit()
        self.assertTrue(self.prepare().prep_id)

    def test_request_after_a_result_needs_next_step(self):
        self.finish_one()
        wid = self.follow_on()
        with self.assertRaises(P.PrepareError) as cm:
            self.prepare(wid)
        self.assertTrue(any("next_step: required" in r for r in cm.exception.reasons))
        self.assertEqual(len(self.store.list_preparations()), 1)

    def test_justification_is_on_the_card(self):
        self.finish_one()
        m = self.req("req-3", work_id="w-3", next_step=dict(self.NS),
                     semantics={"models": {"session": "model-x"},
                                "run_id": "followon-run-0003"})
        self.submit(m)
        res = self.prepare("w-3")
        self.assertIn("Next step justification", res.card)
        for v in self.NS.values():
            self.assertIn(v, res.card)
        self.assertEqual(res.card, res.card_path.read_text())
        # Its own go: the earlier approval's code never approves it.
        with self.assertRaises(AP.ApprovalError):
            AP.approve(self.store, res.prep_id, code="0" * 12, source="chat",
                       quote="go 000000000000", env=self.env)

    def test_stopping_needs_no_launch(self):
        self.finish_one()
        before = (len(self.store.list_messages()), len(self.store.list_preparations()),
                  len(self.store.list_invocations()))
        # Stopping is a plan.md step-log entry: no request, so nothing to prepare
        # and nothing that could launch. The store has no pending work.
        self.assertEqual(before, (1, 1, 1))
        self.assertEqual([i["state"] for i in self.store.list_invocations()],
                         ["completed"])


class SpotlightsToNous(R7.Mixin, R7.SpotBase):
    """Spec §14: a Spotlights result -> a separately justified, separately
    approved Nous request. The Spotlights result starts nothing; the Nous
    request is refused without next_step, and with it gets its own card."""

    def setUp(self):
        super().setUp()
        (self.repo / "investigations" / "s1").mkdir(parents=True)

    def test_follow_on_nous_request_needs_justification_and_its_own_go(self):
        prep = self.prepare()
        self.place(self.record(prep), "candidates-found")
        inv = self.invoke(prep)
        b, _ = self.build(inv)              # NoLaunch guard: nothing starts
        cand = b["findings"]["candidates"][0]
        self.assertEqual(len(self.store.list_preparations()), 1)

        fa, restore = fake.use()            # the follow-on capability is nous
        self.addCleanup(restore)
        sem = {"models": {"session": "model-x"}, "run_id": "spot-to-nous-0001"}
        m = R2.request("req-nous", work_id="w-nous", semantics=sem,
                       question=f"Does candidate {cand['id']} matter in BLIS?")
        m["target"]["commit"] = self.commit
        self.assertTrue(self.store.submit(m, env=self.env).ok)
        with self.assertRaises(P.PrepareError) as cm:
            P.prepare(self.store, "w-nous", target=self.repo, env=self.env)
        self.assertTrue(any("next_step" in r for r in cm.exception.reasons))

        ns = dict(R7.NEXT_STEP, interpretation_of_prior=f"Spotlights found {cand['id']}")
        m2 = R2.request("req-nous-2", work_id="w-nous", revision=2, semantics=sem,
                        question=m["body"]["question"], next_step=ns)
        m2["target"]["commit"] = self.commit
        self.assertTrue(self.store.submit(m2, env=self.env).ok)
        res = P.prepare(self.store, "w-nous", target=self.repo, env=self.env)
        self.assertIn(cand["id"], res.card)
        self.assertNotEqual(res.short_code, prep.short_code)
        rv = P.revalidate(self.store, res.prep_id, env=self.env)
        self.assertFalse(rv.ok)             # no go yet: nothing can launch
        self.assertTrue(any("no approval" in p for p in rv.problems))
        self.assertEqual(len(self.store.list_invocations()), 1)


class InstallSmoke(unittest.TestCase):
    def test_install_smoke_is_wired_into_ci(self):
        smoke = "tests/_runtime_install_smoke.py"
        self.assertTrue((REPO / smoke).is_file())
        local = (REPO / "scripts" / "ci-local.sh").read_text()
        ci = (REPO / ".github" / "workflows" / "ci.yml").read_text()
        self.assertIn(f'"$REPO/{smoke}" .claude/skills/investigation', local)
        self.assertIn(f'"$GITHUB_WORKSPACE/{smoke}" .claude/skills/investigation', ci)
        self.assertFalse((REPO / "skills" / "investigation" / "scripts" /
                          "_runtime_install_smoke.py").exists())

    def test_shipped_entry_points_write_no_bytecode(self):
        # A project-level install lives inside the target repo; __pycache__
        # there would make the target dirty and every prepare refuse (D4).
        scripts = REPO / "skills" / "investigation" / "scripts"
        for p in sorted(scripts.glob("*.py")):
            text = p.read_text()
            if "\nsys.path.insert(0" in text:          # module-level sibling imports
                with self.subTest(script=p.name):
                    self.assertLess(text.index("\nsys.dont_write_bytecode = True"),
                                    text.index("\nsys.path.insert(0"))
        # validate_answer.py stays unchanged (R7); the runtime runs it with -B.
        from ains_runtime import results
        import inspect
        self.assertIn('sys.executable, "-B", str(VALIDATOR)', inspect.getsource(results))


if __name__ == "__main__":
    unittest.main()
