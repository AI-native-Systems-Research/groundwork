"""Guard and failure branches of ains_runtime/results.py (94% at main).

Two layers, on purpose:

  * harness tests reuse the Nous fixtures exactly as test_runtime_results.py
    drives them (prepared + approved + reserved + exited, nothing launched) to
    cover the failure faces that only a real invocation row can reach —
    `_collect`'s post-failure secret re-check, `run_validator`'s
    answer-missing face, and the CLI-visible shape of `_outcome`'s refusals;
  * unit tests exercise the pure helpers directly (`_outcome`, `_ref`,
    `_execution`, `_limitations`, `_limits`, `_pick_answer`,
    `_merge_nous_bundle`, `spotlights_estimates`) and the two defensive
    guards that public flow cannot reach (`_collect`'s adapter_output scrub,
    `build`'s contracts refusal) through their documented injection points.

Lines covered (never-reached at main 8d40c69, CI run 43504194546):
199, 288, 329-331, 335, 363, 376-379, 410-411, 427-428, 436, 453, 475,
489, 491, 496, 545, 613. Line 77 (the sys.path insert) is exercised by the
FreshImport child and provably executes there, but no coverage core records
an event for the one-line if-body, so the file measures 99%, not 100%.

Run from the repo root:  python3 -m unittest tests.test_runtime_results_guards -v
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tests.test_runtime_results as R7  # noqa: E402
from _runtime_util import REPO  # noqa: E402
from ains_runtime import results as RS  # noqa: E402
from ains_runtime import store as S  # noqa: E402
from ains_runtime import supervisor as SV  # noqa: E402

SECRET = "sk-planted-results-guards-4f5a6b7c8d9e0f"


# --- harness: failure faces of a real invocation row --------------------------------

class CollectGuards(R7.NousResults):
    """`_collect`'s after-the-failure secret re-check and the output scrub."""

    def test_secret_echoed_in_a_failure_reason_is_not_recorded(self):
        # Lines 329-331. The post-exception re-check exists because the
        # failure reason embeds the adapter's exception message, which may
        # have echoed a secret value (dd78d0d's follow-through): a collection
        # that fails with a secret in its reason must fail SECRET-FREE, with
        # no adapter output recorded.
        self.env["FAKE_API_TOKEN"] = SECRET
        inv, _ = self.run_fixture("workdir-max-iterations")
        with mock.patch.object(self.adapter, "collect",
                               side_effect=OSError(f"engine died with {SECRET}")):
            out = RS.collect(self.store, inv, env=self.env, study=self.study)
        self.assertEqual(out["collection"]["state"], "failed")
        self.assertEqual(out["collection"]["reason"],
                         "collection output contained a secret value; not recorded")
        self.assertIsNone(out["adapter_output"])
        for e in self.store.list_events(entity_id=inv):
            self.assertNotIn(SECRET, json.dumps(e))

    def test_secret_only_in_adapter_output_scrubs_it_and_keeps_the_rest(self):
        # Line 335 — a defensive guard: the raw output already passed the
        # line-314 check, so a hit here is only reachable when the check
        # itself would miss it (a future regression in find_secret_values).
        # The guard must scrub adapter_output and still record the collect
        # event, never crash.
        inv, _ = self.run_fixture("workdir-max-iterations")
        real = RS.find_secret_values

        def hit_only_the_output(obj, env):
            # the scrub input is the raw output minus "bundle": it keeps
            # native_run_id but loses the bundle the line-314 check saw.
            if (isinstance(obj, dict) and "native_run_id" in obj
                    and "bundle" not in obj):
                return ["$: contains the value of $FAKE_API_TOKEN"]
            return real(obj, env)

        with mock.patch.object(RS, "find_secret_values", side_effect=hit_only_the_output):
            with R7.NoLaunch(self):
                out = RS.collect(self.store, inv, env=self.env, study=self.study)
        self.assertEqual(out["collection"]["state"], "collected")
        self.assertIsNone(out["adapter_output"])
        # The collect event was still written (the run is recorded).
        evs = [e["event"] for e in self.store.list_events(entity_id=inv)]
        self.assertIn("collect:result", evs)


class ValidatorGuards(R7.NousResults):
    """`run_validator`'s not_run / crashed faces through a real invocation."""

    def test_missing_answer_file_is_not_run_with_the_reason(self):
        # Line 363: --answer names a path that does not exist. Never a pass:
        # not_run with the path in the reason.
        inv, _ = self.run_fixture("workdir-max-iterations")
        with R7.NoLaunch(self):
            res = RS.build(self.store, inv, env=self.env,
                           answer=str(self.study / "final" / "gone.json"))
        v = res["message"]["body"]["validation"]
        self.assertEqual(v["state"], "not_run")
        self.assertEqual(v["validator"], "validate_answer.py")
        self.assertIn("gone.json", v["reason"])
        self.assertIn("does not exist", v["reason"])

    def test_validator_timeout_and_crash_are_failed_never_passed(self):
        # Lines 376-379: TimeoutExpired and OSError both become `failed` with
        # "did not finish" reasons, and the report file is still written (the
        # evidence of the attempt is kept, spec §6).
        inv, _ = self.run_fixture("workdir-max-iterations")
        (self.study / "final").mkdir(exist_ok=True)
        ans = self.study / "final" / "answer.json"
        ans.write_text("{}")
        for exc, word in ((subprocess.TimeoutExpired(cmd="x", timeout=5), "timed out"),
                          (OSError("no such interpreter"), "could not run")):
            with self.subTest(word=word):
                with mock.patch.object(RS.subprocess, "run", side_effect=exc):
                    with R7.NoLaunch(self):
                        res = RS.build(self.store, inv, env=self.env,
                                       answer=str(ans))
                v = res["message"]["body"]["validation"]
                self.assertEqual(v["state"], "failed")
                self.assertIsNone(v["exit_code"])
                self.assertIn(word, v["reason"])
                self.assertTrue(Path(v["report_ref"]["location"]).is_file())


class OutcomeRefusals(R7.NousResults):
    """`_outcome`'s three coordinator-refusal arms, CLI-visible."""

    def invoke_completed(self):
        inv, _ = self.run_fixture("workdir-max-iterations")
        return inv

    def test_bad_outcome_class_is_refused(self):
        # Line 489: --outcome outside COORDINATOR_OUTCOMES.
        inv = self.invoke_completed()
        with R7.NoLaunch(self), self.assertRaises(RS.ResultError) as cm:
            RS.build(self.store, inv, env=self.env, outcome=("definitely", "why not"))
        self.assertIn("--outcome must be one of", str(cm.exception))
        self.assertIn("supported, not_supported, inconclusive, other", str(cm.exception))

    def test_blank_explanation_is_refused(self):
        # Line 491: --outcome with only whitespace for --explanation.
        inv = self.invoke_completed()
        with R7.NoLaunch(self), self.assertRaises(RS.ResultError) as cm:
            RS.build(self.store, inv, env=self.env, outcome=("other", "   "))
        self.assertIn("--explanation is required with --outcome", str(cm.exception))

    def test_outcome_over_an_uncollected_invocation_is_refused(self):
        # Line 496: a coordinator outcome needs something collected to
        # classify. A completed execution whose collection failed (the
        # adapter crashed mid-collect) passes the completion pre-check, then
        # the real _outcome call refuses on the collection state.
        inv, _ = self.run_fixture("workdir-max-iterations")
        with mock.patch.object(self.adapter, "collect", side_effect=OSError("boom")):
            with R7.NoLaunch(self), self.assertRaises(RS.ResultError) as cm:
                RS.build(self.store, inv, env=self.env, outcome=("other", "no data"))
        self.assertIn("collection is failed", str(cm.exception))
        self.assertIn("nothing collected to classify", str(cm.exception))

    def test_answer_flag_short_circuits_the_search(self):
        # Line 545: an explicit --answer path wins over both the study's
        # final/answer.json and the engine's answer — and its --root is None
        # (the validator's default root applies).
        inv, _ = self.run_fixture("workdir-max-iterations")
        (self.study / "final").mkdir(exist_ok=True)
        mine = self.study / "final" / "mine.json"
        mine.write_text("{}")
        with R7.NoLaunch(self):
            res = RS.build(self.store, inv, env=self.env, answer=str(mine))
        v = res["message"]["body"]["validation"]
        self.assertEqual(v["answer_ref"]["location"], str(mine.resolve()))
        # The engine's answer.json is NOT the one validated.
        self.assertNotIn("workdir", v["answer_ref"]["location"])


class BuildContractRefusal(R7.NousResults):
    def test_a_contract_refusal_raises_store_error_and_records_no_result(self):
        # Line 613 — build()'s last line of defence: the record it assembled
        # was refused by contracts.py. Public flow cannot reach it (build
        # constructs a valid message by construction); the guard is covered
        # at its injection point. Nothing may be recorded when it fires.
        inv, _ = self.run_fixture("workdir-max-iterations")
        RS.collect(self.store, inv, env=self.env)   # classify first: exited -> completed
        before = [e["event"] for e in self.store.list_events(entity_id=inv)]
        with mock.patch.object(RS, "validate_message",
                               return_value=["body.answer: fabricated"]):
            with R7.NoLaunch(self), self.assertRaises(S.StoreError) as cm:
                RS.build(self.store, inv, env=self.env)
        self.assertIn("refused by contracts.py", str(cm.exception))
        self.assertIn("fabricated", str(cm.exception))
        after = [e["event"] for e in self.store.list_events(entity_id=inv)]
        self.assertNotIn("result:built", after)
        # the only new event is this attempt's collect: the run was recorded,
        # the refused result was not
        self.assertEqual(after[len(before):], ["collect:result"])


# --- unit: the pure helpers ----------------------------------------------------------

class OutcomeUnit(unittest.TestCase):
    """_outcome's refusal arms, called directly (no store needed)."""

    def setUp(self):
        self.norm = RS.Normalized("collected", explanation="e")
        self.execution = {"state": "completed"}
        self.record = {"request_message_id": "req-1",
                       "material": {"request": {"hypothesis": "32 wins"}}}

    def test_unknown_class_names_the_allowed_set(self):
        with self.assertRaises(RS.ResultError) as cm:
            RS._outcome(self.norm, self.execution, self.record, ("sideways", "x"))
        self.assertIn("--outcome must be one of supported, not_supported, "
                      "inconclusive, other", str(cm.exception))

    def test_blank_explanation_is_rejected(self):
        for bad in ("", "   ", None):
            with self.subTest(bad=bad):
                with self.assertRaises(RS.ResultError) as cm:
                    RS._outcome(self.norm, self.execution, self.record, ("other", bad))
                self.assertIn("--explanation is required with --outcome",
                              str(cm.exception))

    def test_uncollected_collection_is_rejected(self):
        with self.assertRaises(RS.ResultError) as cm:
            RS._outcome(RS.Normalized("failed", "adapter crashed"), self.execution,
                        self.record, ("other", "x"))
        self.assertIn("collection is failed: nothing collected to classify",
                      str(cm.exception))


class RefHelpers(unittest.TestCase):
    def test_no_location_is_none(self):
        self.assertIsNone(RS._ref("artifact", None))
        self.assertIsNone(RS._ref("artifact", ""))

    def test_a_directory_is_a_provisional_ref(self):
        # exists() but not is_file(): provisional, no digest.
        with tempfile.TemporaryDirectory() as td:
            r = RS._ref("evidence-dir", td)
            self.assertEqual(r["location"], td)
            self.assertTrue(r["provisional"])
            self.assertNotIn("digest", r)

    def test_unmakeable_ref_is_none_not_a_crash(self):
        # Lines 410-411: make_ref raises (a file that disappears between
        # is_file() and hashing, or a bad kind) — the guard returns None so
        # one bad location cannot kill the whole record.
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "gone.txt"
            p.write_text("x")

            def vanishing(*a, **kw):
                p.unlink()
                return RS.make_ref(*a, **kw)

            real = RS.make_ref
            # a file deleted between the is_file() check and the digest read
            with mock.patch.object(RS, "make_ref", side_effect=ValueError("boom")):
                self.assertIsNone(RS._ref("artifact", p))
            with mock.patch.object(RS, "make_ref", side_effect=OSError("gone")):
                self.assertIsNone(RS._ref("artifact", p))
            real  # quiet the linter; the real call is exercised by other tests


class ExecutionHelpers(unittest.TestCase):
    """_execution's diagnostics assembly and the unknown exit code."""

    class FakeStore:
        def __init__(self, events):
            self._events = events

        def list_events(self, entity_id):
            return self._events

    def make_inv(self, **over):
        inv = {"inv_id": "inv-x", "state": "completed", "launch": {"logs": {}},
               "native": {}, "engine_pid": None}
        inv.update(over)
        return inv

    def test_log_and_evidence_locations_become_refs(self):
        # Lines 427-428: every launch log location gets a ref; 429-433: the
        # native evidence list too, deduped against the logs by location.
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "engine.log"
            log.write_text("ran\n")
            ev = Path(td) / "evidence.json"
            ev.write_text("{}")
            store = self.FakeStore([{"event": "state:running", "at": "t0"}])
            inv = self.make_inv(
                launch={"logs": {"engine": str(log), "empty": None,
                                 "missing": str(Path(td) / "nope.log")}},
                native={"evidence": [{"kind": "report", "location": str(ev)},
                                     {"kind": "dupe", "location": str(log)},
                                     {"kind": "none", "location": None}],
                        "native_stop_reason": "halt"})
            e = RS._execution(store, inv)
            self.assertEqual(e["state"], "completed")
            self.assertEqual(e["started_at"], "t0")
            self.assertEqual(e["native_stop_reason"], "halt")
            locs = [d["location"] for d in e["diagnostics"]]
            self.assertEqual(locs, [str(log), str(ev)])  # missing/None dropped, dupe deduped
            kinds = [d["kind"] for d in e["diagnostics"]]
            self.assertEqual(kinds, ["engine-log", "report"])

    def test_terminal_with_engine_pid_and_no_exit_code_is_unknown(self):
        # Line 436: the engine ran but its exit was never recorded (a
        # supervisor crash after spawn): exit_code reads "unknown", never a
        # fabricated 0.
        store = self.FakeStore([])
        e = RS._execution(store, self.make_inv(engine_pid=4242))
        self.assertEqual(e["exit_code"], "unknown")
        # non-terminal or no engine: stays None
        self.assertIsNone(RS._execution(store, self.make_inv(state="running",
                                                             engine_pid=1))["exit_code"])
        self.assertIsNone(RS._execution(store, self.make_inv())["exit_code"])
        self.assertEqual(RS._execution(store, self.make_inv(exit_code=3))["exit_code"], 3)


class LimitationHelpers(unittest.TestCase):
    def test_native_warnings_become_runtime_limitations(self):
        # Line 453: the adapter's own warnings surface as limitations the
        # owner sees, attributed to runtime:classify.
        lim = RS._limitations(RS.Normalized("collected"),
                              {"native": {"warnings": ["cache degraded", ""]}})
        texts = {(x["source"], x["text"]) for x in lim}
        self.assertIn(("runtime:classify", "cache degraded"), texts)

    def test_partial_note_names_the_native_reason_when_there_is_one(self):
        norm = RS.Normalized("collected", partial=True)
        with_reason = RS._limitations(norm, {"native": {"reason": "halt"}})
        self.assertIn("findings are partial: halt",
                      [x["text"] for x in with_reason])
        without = RS._limitations(norm, {"native": {}})
        self.assertIn("findings are partial", [x["text"] for x in without])

    def test_duplicate_limitations_are_deduped(self):
        norm = RS.Normalized("collected", limitations=[
            {"source": "a", "text": "same"}, {"source": "a", "text": "same"},
            {"source": "b", "text": "other"}])
        lim = RS._limitations(norm, {"native": {"warnings": ["same"[:0] + "other"]}})
        self.assertEqual([(x["source"], x["text"]) for x in lim],
                         [("a", "same"), ("b", "other"), ("runtime:classify", "other")])

    def test_non_dict_limitations_are_dropped(self):
        norm = RS.Normalized("collected", limitations=["just a string", {"text": ""}])
        self.assertEqual(RS._limitations(norm, {"native": {}}), [])


class LimitsHelpers(unittest.TestCase):
    def test_a_time_budget_carries_its_observed_elapsed(self):
        # Line 475: an adapter-labelled time budget the invocation actually
        # ran under reports the observed elapsed_s next to the budget.
        inv = {"created_at": "2026-01-01T00:00:00.000000Z",
               "ended_at": "2026-01-01T00:10:00.000000Z"}
        record = {"material": {"limits": [
            {"name": "max_wall_s", "value": 3600, "enforced_by": "adapter"},
            {"name": "max_iterations", "value": 5, "enforced_by": "adapter"}]}}
        out = {x["name"]: x for x in RS._limits(inv, record)}
        self.assertEqual(out["max_wall_s"]["observed"], 600.0)
        self.assertNotIn("observed", out["max_iterations"])
        # the runtime's own guards are always listed
        self.assertIn("approvals_per_invocation", out)

    def test_note_survives_when_the_adapter_wrote_one(self):
        record = {"material": {"limits": [
            {"name": "max_iterations", "value": 2, "enforced_by": "adapter",
             "note": "hard stop"}]}}
        out = {x["name"]: x for x in RS._limits({}, record)}
        self.assertEqual(out["max_iterations"]["note"], "hard stop")


class SmallGuards(unittest.TestCase):
    def test_merge_nous_bundle_needs_a_study_a_dir_and_a_bundle(self):
        # Line 288: no study / a missing study dir / no bundle each mean
        # "nothing to merge", returned as None (the collect note is skipped).
        self.assertIsNone(RS._merge_nous_bundle({"bundle": {}}, None))
        self.assertIsNone(RS._merge_nous_bundle(
            {"bundle": {}}, Path(tempfile.gettempdir()) / "no-such-dir"))
        self.assertIsNone(RS._merge_nous_bundle({"state": "collected"}, "/tmp"))

    def test_spotlights_estimates_tolerates_a_non_dict_result(self):
        # Line 199: a result.json that parses to a list (or no result.json at
        # all, or no output folder) yields an empty guard set rather than a
        # crash — nothing collected means nothing to forbid.
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "result.json").write_text("[1, 2]")
            self.assertEqual(RS.spotlights_estimates(
                {"non_material": {"output_folder": td}}), set())
        self.assertEqual(RS.spotlights_estimates({}), set())
        self.assertEqual(RS.spotlights_estimates(
            {"non_material": {"output_folder": ""}}), set())

    def test_pick_answer_prefers_the_explicit_flag(self):
        # Line 545: --answer wins outright; its root is None (the validator's
        # own default root resolution applies).
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "mine.json"
            p.write_text("{}")
            self.assertEqual(RS._pick_answer(str(p), "/no/study", RS.Normalized("x")),
                             (p.resolve(), None))
            # study final/answer.json next, then the engine's answer
            study = Path(td) / "s"
            (study / "final").mkdir(parents=True)
            (study / "final" / "answer.json").write_text("{}")
            self.assertEqual(RS._pick_answer(None, str(study), RS.Normalized("x")),
                             ((study / "final" / "answer.json").resolve(), None))


class FreshImport(unittest.TestCase):
    def test_import_adds_the_scripts_dir_for_sibling_modules(self):
        # Line 77 exists for the fresh-interpreter shape: a process that
        # imports ains_runtime.results with the scripts dir reachable only as
        # a relative sys.path entry (cwd) must end up with the ABSOLUTE
        # scripts dir on sys.path, so a later `import collect_spotlights`
        # (the documented sibling-module import: _collection, _config,
        # collect_spotlights are imported by name from these modules) works
        # no matter what the cwd is later. The test process imports through
        # tests/_runtime_util.py, which puts SCRIPTS on the path before
        # ains_runtime is ever imported, so this runs only in a child.
        code = ("import sys\n"
                "import ains_runtime.results as RS\n"
                "assert str(RS.SCRIPTS) in sys.path, RS.SCRIPTS\n"
                "import os\n"
                "os.chdir('/')\n"          # leave the scripts dir behind
                "import collect_spotlights  # sibling module, by name\n"
                "print('ok')\n")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, cwd=REPO / "skills" / "investigation" / "scripts")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ok", r.stdout)


if __name__ == "__main__":
    unittest.main()
