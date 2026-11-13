"""R7: results and evidence integration (plan §4.6, §5 R7; spec §11).

Drives real preparations through the shipped Nous and Spotlights static
adapters (their engine hooks injected, as in R4/R5's tests), reserves the
invocation directly and records its exit, so no supervisor or engine is ever
spawned. Native output comes from the R4/R5 fixtures. Every test that builds
a result also asserts nothing was (re)launched.
"""
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runtime_util import RUNTIME  # noqa: E402
from ains_runtime import adapters as A  # noqa: E402
from ains_runtime import approval as AP  # noqa: E402
from ains_runtime import results as RS  # noqa: E402
from ains_runtime import store as S  # noqa: E402
from ains_runtime import supervisor as SV  # noqa: E402
from ains_runtime.adapters.base import NativeState  # noqa: E402
from ains_runtime.contracts import CLAIM_KEYS, validate_message  # noqa: E402
from ains_runtime.export import export_study  # noqa: E402
from test_runtime_nous import FIX as NOUS_FIX  # noqa: E402
from test_runtime_nous import Base as NousBase  # noqa: E402
from test_runtime_prepare import go  # noqa: E402
from test_runtime_spotlights_static import Base as SpotBase  # noqa: E402

import collect_spotlights as CS  # noqa: E402


def snapshot(root: Path) -> dict:
    if not root.exists():
        return {}
    return {str(p): (p.stat().st_mtime_ns, p.stat().st_size,
                     p.read_bytes() if p.is_file() else None)
            for p in sorted(root.rglob("*"))}


class NoLaunch:
    """Fails the test if anything starts a supervisor or an engine. Other
    subprocesses (git, validate_answer.py) are allowed and recorded."""

    ENGINES = ("engine", "nous", "spotlights-engine", "signal-pipeline")

    def __init__(self, case):
        self.case = case
        self.spawned: list = []

    def __enter__(self):
        boom = mock.Mock(side_effect=AssertionError("results must never launch"))
        real = subprocess.Popen
        me = self

        class Guard(real):
            def __init__(self, args, *a, **kw):
                argv = [str(x) for x in (args if isinstance(args, (list, tuple))
                                         else [args])]
                me.spawned.append(argv)
                if "_supervise" in argv or Path(argv[0]).name in me.ENGINES:
                    raise AssertionError(f"results must never launch: {argv}")
                super().__init__(args, *a, **kw)
        self.patches = [mock.patch.object(SV, "spawn_supervisor", boom),
                        mock.patch.object(SV, "launch", boom),
                        mock.patch.object(SV, "supervise", boom),
                        mock.patch.object(subprocess, "Popen", Guard)]
        for p in self.patches:
            p.start()
        self.before = [i["inv_id"] for i in self.case.store.list_invocations()]
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()
        self.case.assertEqual(self.before,
                              [i["inv_id"] for i in self.case.store.list_invocations()],
                              "a result created an invocation")


NEXT_STEP = {"interpretation_of_prior": "the prior run settled one arm",
             "remaining_uncertainty": "the other arm",
             "expected_information_gain": "settles the other arm",
             "rationale": "test: a follow-on request in the same study"}


class Mixin:
    """Prepared + approved + reserved + exited, without spawning anything."""

    def invoke(self, prep, *, rc=0, cancel=False, state=SV.EXITED):
        AP.approve(self.store, prep.prep_id, code=prep.short_code, source="chat",
                   quote=go(prep.short_code), env=self.env)
        inv_id = f"inv-{prep.prep_id[-8:]}"
        self.store.reserve_invocation(inv_id, prep.prep_id, launch={
            "argv": ["engine"], "logs": SV.log_paths(self.store, inv_id)})
        if cancel:
            self.store.request_cancel(inv_id, {"reason": "test"})
        if state != SV.RESERVED:
            self.store.transition(inv_id, (SV.RESERVED,), state, exit_code=rc,
                                  ended_at=S.now())
        return inv_id

    def build(self, inv_id, **kw):
        with NoLaunch(self):
            res = RS.build(self.store, inv_id, env=self.env, **kw)
        msg = res["message"]
        self.assertEqual(validate_message(msg, {}), [])
        for c in (msg["body"].get("findings") or {}).get("claims", []):
            self.assertEqual(tuple(c), CLAIM_KEYS)
        return msg["body"], res


# --- Nous -----------------------------------------------------------------------------

class NousResults(Mixin, NousBase):
    def setUp(self):
        super().setUp()
        self.study = self.repo / "investigations" / "s1"
        self.study.mkdir(parents=True)

    def run_fixture(self, fixture, *, rc=0, cancel=False, **body_over):
        m = self.submit(self.msg(**body_over))
        prep = self.prepare(m["work_id"])
        rec = self.store.get_preparation(prep.prep_id)["record"]
        wd = Path(rec["env_set"]["NOUS_CAMPAIGN_PARENT"]) / rec["native_run_id"]
        shutil.copytree(NOUS_FIX / fixture, wd)
        return self.invoke(prep, rc=rc, cancel=cancel), wd

    def test_inconclusive_and_negative_findings_are_completed_executions(self):
        inv, wd = self.run_fixture("workdir-max-iterations")
        b, _ = self.build(inv)
        self.assertEqual(b["execution"]["state"], "completed")
        self.assertEqual(b["execution"]["native_stop_reason"], "max_iterations")
        self.assertEqual(b["collection"], {"state": "collected", "reason": None})
        cls = {(h["iteration"], h["id"]): h["classification"]
               for h in b["findings"]["hypotheses"]}
        self.assertEqual(cls[(1, "H2")], "not_supported")
        self.assertEqual(cls[(2, "H1")], "inconclusive")
        self.assertFalse(b["findings"]["partial"])
        self.assertEqual(b["findings"]["claims"][0]["id"], "C1")
        # Nous has no study-level verdict: unavailable, with a reason.
        self.assertEqual(b["outcome"]["classification"], "unavailable")
        self.assertTrue(b["outcome"]["explanation"].strip())
        self.assertEqual(b["outcome"]["classified_by"], "runtime")
        self.assertEqual(b["spend"]["completeness"], "partial")
        self.assertIn("coordinator", {x["source"] for x in b["limitations"]})
        self.assertTrue(any(x["name"] == "max_iterations" for x in b["limits"]))
        self.assertTrue(b["versions"]["adapter"].startswith("nous/"))
        # The bundle reached collection.json like `collect.py --study`.
        self.assertIn("nous", json.loads((self.study / "collection.json").read_text()))

    def next_step_after_first(self):
        """A second request in a study with a result needs next_step (plan §4.1)."""
        return {"next_step": NEXT_STEP} if self.store.list_invocations() else {}

    def test_coordinator_negative_and_inconclusive_outcomes_stay_completed(self):
        for cls in ("not_supported", "inconclusive"):
            with self.subTest(cls):
                inv, _ = self.run_fixture("workdir-max-iterations",
                                          question=f"Does block size 32 beat 16 ({cls})?",
                                          semantics={"run_id": f"nousresult-{cls}-0001"},
                                          **self.next_step_after_first())
                b, _ = self.build(inv, outcome=(cls, "effect within 2x noise in W1-W3"))
                self.assertEqual(b["execution"]["state"], "completed")
                self.assertEqual(b["outcome"]["classification"], cls)
                self.assertEqual(b["outcome"]["classified_by"], "coordinator")
                self.assertTrue(b["outcome"]["hypothesis_ref"].endswith(":body.hypothesis"))

    def test_coordinator_outcome_refused_without_hypothesis_or_completion(self):
        inv, _ = self.run_fixture("workdir-max-iterations", hypothesis=None)
        with self.assertRaises(RS.ResultError) as cm:
            RS.build(self.store, inv, env=self.env, outcome=("supported", "x"))
        self.assertIn("hypothesis", str(cm.exception))
        b, _ = self.build(inv, outcome=("other", "a sweep, not a hypothesis test"))
        self.assertEqual(b["outcome"]["classification"], "other")
        inv2, _ = self.run_fixture("workdir-halt-design",
                                   semantics={"run_id": "nousresult-halt-0002"},
                                   next_step=NEXT_STEP)
        with self.assertRaises(RS.ResultError) as cm:
            RS.build(self.store, inv2, env=self.env, outcome=("not_supported", "x"))
        self.assertIn("only for a completed execution", str(cm.exception))

    def test_halt_is_failed_with_partial_findings(self):
        inv, _ = self.run_fixture("workdir-halt-design")
        b, _ = self.build(inv)
        self.assertEqual(b["execution"]["state"], "failed")
        self.assertEqual(b["execution"]["native_stop_reason"], "halt")
        self.assertTrue(b["findings"]["partial"])
        self.assertTrue(all(h["partial"] for h in b["findings"]["hypotheses"]))
        self.assertTrue(b["outcome"]["partial"])
        self.assertEqual(b["outcome"]["classification"], "unavailable")
        self.assertIn("execution failed", b["outcome"]["explanation"])
        self.assertIn("runtime", {x["source"] for x in b["limitations"]})

    def test_failure_without_partial_evidence_carries_no_findings(self):
        inv, _ = self.run_fixture("workdir-no-baseline")
        b, _ = self.build(inv)
        self.assertEqual(b["execution"]["state"], "failed")
        self.assertEqual(b["execution"]["native_stop_reason"], "no_baseline")
        self.assertNotIn("findings", b)
        self.assertEqual(b["outcome"]["classification"], "unavailable")
        self.assertIn("no_baseline", b["outcome"]["explanation"])
        self.assertIsNone(b["spend"]["reported_usd"])

    def test_collection_failure_preserves_evidence_and_never_reruns(self):
        inv, wd = self.run_fixture("workdir-max-iterations")
        RS.collect(self.store, inv, env=self.env)       # classify (exited -> completed)
        row = self.store.get_invocation(inv)
        native = snapshot(wd)
        with mock.patch.object(self.adapter, "collect",
                               side_effect=OSError("disk went away")):
            b, _ = self.build(inv)
        self.assertEqual(b["collection"]["state"], "failed")
        self.assertIn("disk went away", b["collection"]["reason"])
        self.assertEqual(b["execution"]["state"], "completed")
        self.assertNotIn("findings", b)
        self.assertEqual(b["outcome"]["classification"], "unavailable")
        self.assertIn("collection failed", b["outcome"]["explanation"])
        after = self.store.get_invocation(inv)
        for k in ("state", "exit_code", "ended_at", "native", "engine_pid", "prep_id"):
            self.assertEqual(row[k], after[k], k)
        self.assertEqual(native, snapshot(wd))
        # Retry: collection is read-only and repeatable.
        b2, _ = self.build(inv)
        self.assertEqual(b2["collection"]["state"], "collected")
        self.assertEqual(native, snapshot(wd))
        evs = [e["event"] for e in self.store.list_events(entity_id=inv)]
        self.assertEqual(evs.count("result:built"), 2)

    def test_validation_failure_preserves_evidence_and_never_reruns(self):
        inv, wd = self.run_fixture("workdir-max-iterations")
        native = snapshot(wd)
        (self.study / "final").mkdir()
        bad = {"answer": "x", "deliverable": None, "repro": "true",
               "claims": [{"id": "C1", "metric": "capacity", "value": 9.99,
                           "condition": "bs=32", "evidence": "nope.json"}]}
        (self.study / "final" / "answer.json").write_text(json.dumps(bad))
        b, _ = self.build(inv)
        v = b["validation"]
        self.assertEqual((v["state"], v["exit_code"]), ("failed", 1))
        self.assertIn("does not exist", v["reason"])
        report = Path(v["report_ref"]["location"])
        self.assertIn("nope.json", report.read_text())
        self.assertEqual(b["execution"]["state"], "completed")
        self.assertEqual(b["collection"]["state"], "collected")
        self.assertEqual(native, snapshot(wd))
        # A passing answer: evidence in the study.
        (self.study / "r.json").write_text('{"capacity": 1.2}')
        good = dict(bad, claims=[dict(bad["claims"][0], value=1.2, evidence="r.json")])
        (self.study / "final" / "answer.json").write_text(json.dumps(good))
        b, _ = self.build(inv)
        self.assertEqual((b["validation"]["state"], b["validation"]["exit_code"]),
                         ("passed", 0))

    def test_engine_answer_validated_when_study_has_none(self):
        inv, wd = self.run_fixture("workdir-max-iterations")
        b, _ = self.build(inv)
        v = b["validation"]
        self.assertEqual(v["answer_ref"]["location"], str(wd / "answer.json"))
        self.assertEqual(v["state"], "failed")      # deliverable "none" is not a path
        self.assertEqual(b["execution"]["state"], "completed")

    def test_not_terminal_is_refused_and_recovery_required_is_reported(self):
        m = self.submit(self.msg())
        prep = self.prepare(m["work_id"])
        AP.approve(self.store, prep.prep_id, code=prep.short_code, source="chat",
                   quote=go(prep.short_code), env=self.env)
        from ains_runtime import procid
        me = os.getpid()
        self.store.reserve_invocation("inv-live", prep.prep_id,
                                      launcher=(me, procid.start_time(me)),
                                      launch={"argv": ["engine"]})
        with NoLaunch(self), self.assertRaises(RS.ResultError):
            RS.build(self.store, "inv-live", env=self.env)
        c = RS.collect(self.store, "inv-live", env=self.env)
        self.assertEqual(c["collection"]["state"], "not_run")
        self.store.transition("inv-live", (SV.RESERVED,), SV.RECOVERY_REQUIRED)
        b, _ = self.build("inv-live")
        self.assertEqual(b["execution"]["state"], "recovery_required")
        self.assertEqual(b["collection"]["state"], "not_run")
        self.assertEqual(b["outcome"]["classification"], "unavailable")
        self.assertNotIn("findings", b)

    def test_export_writes_results(self):
        inv, _ = self.run_fixture("workdir-max-iterations")
        self.build(inv)
        export_study(self.store, self.study, env=self.env)
        out = json.loads((self.study / "runtime" / "results" / f"{inv}.json").read_text())
        self.assertEqual(out["grants"], "nothing")
        self.assertEqual(out["record"]["message"]["body"]["invocation_id"], inv)

    def test_secret_in_bundle_only_output_fails_collection_before_merge(self):
        # The Nous bundle (collect.py's answer_summary, guards, ...) reaches
        # <study>/collection.json but not Normalized.summary(); before the
        # fix the secret check ran on the summary only, and only after the
        # merge — so a secret in answer.json's answer text was merged into
        # collection.json verbatim. Now the whole raw adapter output is
        # checked before the merge: the collection fails, nothing is merged.
        self.env["FAKE_API_TOKEN"] = "sk-planted-results-4f5a6b7c8d9e0f"
        m = self.submit(self.msg())
        prep = self.prepare(m["work_id"])
        rec = self.store.get_preparation(prep.prep_id)["record"]
        wd = Path(rec["env_set"]["NOUS_CAMPAIGN_PARENT"]) / rec["native_run_id"]
        shutil.copytree(NOUS_FIX / "workdir-max-iterations", wd)
        a = json.loads((wd / "answer.json").read_text())
        a["answer"] = "Partly. leaked sk-planted-results-4f5a6b7c8d9e0f"
        (wd / "answer.json").write_text(json.dumps(a))
        inv = self.invoke(prep)
        with NoLaunch(self):
            out = RS.collect(self.store, inv, env=self.env, study=self.study)
        secret = "sk-planted-results-4f5a6b7c8d9e0f"
        self.assertEqual(out["collection"]["state"], "failed")
        self.assertEqual(out["collection"]["reason"],
                         "collection output contained a secret value; not recorded")
        self.assertIsNone(out["adapter_output"])
        # Nothing was merged, no secret reached any stored event, the study
        # or the returned dict.
        self.assertFalse((self.study / "collection.json").exists())
        for e in self.store.list_events(entity_id=inv):
            self.assertNotIn(secret, json.dumps(e))
        self.assertNotIn(secret, json.dumps(out))
        self.assertNotIn(secret, (self.study / "collection.json").read_text()
                         if (self.study / "collection.json").exists() else "")
        # The native output itself is kept as is (a failed collection changes
        # nothing else).
        self.assertIn(secret, (wd / "answer.json").read_text())


# --- Spotlights static --------------------------------------------------------------

class SpotlightsResults(Mixin, SpotBase):
    def setUp(self):
        super().setUp()
        self.study = self.repo / "investigations" / "s1"
        self.study.mkdir(parents=True)

    def run_fixture(self, fixture, *, rc=0):
        prep = self.prepare()
        rec = self.record(prep)
        if fixture:
            self.place(rec, fixture)
        return self.invoke(prep, rc=rc), rec

    def estimates(self, rec):
        out = Path(rec["non_material"]["output_folder"])
        res = json.loads((out / "result.json").read_text())
        return CS.estimate_values(res["report"], None,
                                  json.loads((out / "run_manifest.json").read_text()))

    def test_candidates_found_is_other_and_estimates_never_become_claims(self):
        inv, rec = self.run_fixture("candidates-found")
        b, _ = self.build(inv)
        self.assertEqual(b["execution"]["state"], "completed")
        self.assertEqual(b["execution"]["native_stop_reason"], "candidates_found")
        self.assertEqual(b["outcome"]["classification"], "other")
        self.assertIn("not measurements", b["outcome"]["explanation"])
        f = b["findings"]
        self.assertEqual(len(f["claims"]), 3)
        forbidden = self.estimates(rec)
        self.assertIn("high", forbidden)
        for c in f["claims"]:
            self.assertNotIn(str(c["value"]), forbidden)
        self.assertEqual([c["id"] for c in f["candidates"]][:3],
                         ["cand-toy_cache-0001", "cand-toy_cache-0002",
                          "cand-toy_cache-0003"])
        self.assertNotIn("estimated_impact", json.dumps(b))
        self.assertEqual(b["spend"]["completeness"], "partial")
        self.assertTrue(b["versions"]["adapter"].startswith("spotlights-static/"))

    def test_partial_is_labelled(self):
        inv, _ = self.run_fixture("partial-degraded")
        b, _ = self.build(inv)
        self.assertEqual(b["execution"]["state"], "completed")
        self.assertTrue(b["outcome"]["partial"])
        self.assertTrue(b["findings"]["partial"])
        texts = " ".join(x["text"] for x in b["limitations"])
        self.assertIn("DEGRADED", texts)

    def test_no_opportunity_is_a_completed_execution(self):
        inv, _ = self.run_fixture("no-opportunity")
        b, _ = self.build(inv)
        self.assertEqual(b["execution"]["state"], "completed")
        self.assertEqual(b["outcome"]["classification"], "other")
        self.assertIn("no_opportunity", b["outcome"]["explanation"])
        self.assertEqual(b["findings"]["claims"], [])

    def test_failed_run_has_no_findings(self):
        inv, _ = self.run_fixture("failed-unrecoverable", rc=1)
        b, _ = self.build(inv)
        self.assertEqual(b["execution"]["state"], "failed")
        self.assertNotIn("findings", b)
        self.assertEqual(b["collection"]["state"], "not_run")
        self.assertTrue(b["collection"]["reason"])
        self.assertEqual(b["outcome"]["classification"], "unavailable")

    def test_adapter_guard_failure_is_a_collection_failure(self):
        inv, rec = self.run_fixture("candidates-found")
        out = Path(rec["non_material"]["output_folder"])
        native = snapshot(out)

        def leaky(study, run_dir, **kw):
            return ({"claims_draft": [{"id": "cite-x", "value": "high"}], "notes": []},
                    Path(study) / "collection.json")
        with mock.patch.object(CS, "collect_run", leaky):
            b, _ = self.build(inv)
        self.assertEqual(b["collection"]["state"], "failed")
        self.assertIn("estimate", b["collection"]["reason"])
        self.assertNotIn("findings", b)
        self.assertEqual(b["execution"]["state"], "completed")
        self.assertEqual(native, snapshot(out))

    def test_results_guard_catches_an_estimate_the_adapter_missed(self):
        inv, rec = self.run_fixture("candidates-found")
        real = self.adapter.collect

        def sneaky(*a, **kw):
            r = real(*a, **kw)
            r["claims_draft"] = r["claims_draft"] + [
                {"id": "cite-y", "metric": "m", "value": "medium", "condition": "c",
                 "evidence": "citations/x.txt"}]
            r["estimate_guard"] = {"checked": 4, "violations": []}
            return r
        with mock.patch.object(self.adapter, "collect", sneaky):
            b, _ = self.build(inv)
        self.assertEqual(b["collection"]["state"], "failed")
        self.assertIn("cite-y", b["collection"]["reason"])
        self.assertNotIn("findings", b)

    def test_estimate_guard_blocks_alternate_numeric_spellings(self):
        """The 'estimate never becomes a claim value' invariant holds for
        every spelling of the number: a claims_draft value '0.50' against a
        result.json carrying estimated_impact 0.5 fails collection through
        the normalizer (at main only the exact strings '0.5' were blocked,
        so the estimate flowed into an evidence-backed, verified claim)."""
        inv, rec = self.run_fixture("candidates-found")
        out = Path(rec["non_material"]["output_folder"])
        result = json.loads((out / "result.json").read_text())
        result["report"]["candidates"][0]["estimated_impact"] = 0.5
        (out / "result.json").write_text(json.dumps(result))
        real = self.adapter.collect

        def sneaky(*a, **kw):
            r = real(*a, **kw)
            r["claims_draft"] = r["claims_draft"] + [
                {"id": "cite-spelling", "metric": "m", "value": "0.50",
                 "condition": "c", "evidence": "citations/x.txt"}]
            r["estimate_guard"] = {"checked": 4, "violations": []}
            return r
        with mock.patch.object(self.adapter, "collect", sneaky):
            b, _ = self.build(inv)
        self.assertEqual(b["collection"]["state"], "failed")
        self.assertIn("estimate", b["collection"]["reason"])
        self.assertIn("cite-spelling", b["collection"]["reason"])
        self.assertNotIn("findings", b)
        # the same-value control: a claim value NOT equal to any spelling of
        # the estimate stays collected
        with mock.patch.object(self.adapter, "collect", real):
            b, _ = self.build(inv)
        self.assertEqual(b["collection"]["state"], "collected")

    def test_cli_collect_and_result_export(self):
        inv, _ = self.run_fixture("no-opportunity")
        env = {**os.environ, **self.env}
        base = [sys.executable, str(RUNTIME), "--db", str(self.db)]
        r = subprocess.run(base + ["collect", inv], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["collection"]["state"], "collected")
        r = subprocess.run(base + ["result", inv, "--out", str(self.study)],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        p = Path(out["exported_to"])
        self.assertEqual(p, self.study / "runtime" / "results" / f"{inv}.json")
        self.assertEqual(json.loads(p.read_text())["record"]["message"]["body"]
                         ["outcome"]["classification"], "other")
        r = subprocess.run(base + ["result", inv, "--explanation", "x"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(len(self.store.list_invocations()), 1)


# --- normalizer plug-in point (a later adapter, e.g. telemetry, D10) ------------------

class Normalizers(unittest.TestCase):
    def test_generic_fallback_and_adapter_hook(self):
        class Fake:
            capability = "spotlights-telemetry"
        n = RS.normalize(Fake(), {"state": "collected", "partial": True,
                                  "outcome": {"classification": "other",
                                              "reason": "snapshot"},
                                  "claims": [{"id": "C", "metric": "m", "value": 1,
                                              "condition": "c", "evidence": "e",
                                              "verified": True}]}, {})
        self.assertEqual((n.state, n.outcome, n.partial), ("collected", "other", True))
        self.assertEqual(tuple(n.claims[0]), CLAIM_KEYS)

        class Hooked(Fake):
            def normalize_collection(self, collected, record):
                return RS.Normalized("unavailable", "custom")
        self.assertEqual(RS.normalize(Hooked(), {}, {}).reason, "custom")
        n = RS.normalize(Fake(), {"state": "weird"}, {})
        self.assertEqual(n.state, "failed")
        self.assertTrue(n.reason)

    def test_shipped_normalizers_registered(self):
        self.assertEqual(set(RS.NORMALIZERS) & set(A.names()),
                         {"nous", "spotlights-static"})
        self.assertIsInstance(NativeState("completed"), NativeState)


if __name__ == "__main__":
    unittest.main()
