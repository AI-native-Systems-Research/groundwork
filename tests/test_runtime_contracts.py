"""ains_runtime.contracts: every message type valid + malformed; versions."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _runtime_util import (DIGEST, EXAMPLES, SCHEMAS, envelope, mutate,  # noqa: E402
                           request)
from ains_runtime import SCHEMA_VERSIONS  # noqa: E402
from ains_runtime.artifacts import validate_artifact_ref  # noqa: E402
from ains_runtime.contracts import (ContractError, check_message, parse_schema,  # noqa: E402
                                    validate_message)

NOENV = {}

VALID = {
    "experiment-request": request(),
    "receipt": envelope("receipt", "rc-1", {
        "received_message_id": "req-1", "received_schema": "ains.experiment-request/v1",
        "payload_digest": DIGEST, "status": "accepted", "work_state": "accepted",
        "reasons": [], "received_at": "2026-11-09T18:00:00Z"}, reply_to="req-1"),
    "progress": envelope("progress", "p-1", {"state": "running",
                                             "at": "2026-11-09T18:00:00Z"}),
    "clarification": envelope("clarification", "c-1",
                              {"kind": "question", "text": "Which trace?"}),
    "cancel": envelope("cancel", "x-1", {"reason": "owner asked"}),
    "experiment-result": json.loads((EXAMPLES / "experiment-result.json").read_text()),
}

# (type, description, mutation) -> must be invalid
MALFORMED = [
    ("experiment-request", "missing question", lambda m: m["body"].pop("question")),
    ("experiment-request", "empty objective", lambda m: m["body"].update(objective=" ")),
    ("experiment-request", "unknown capability", lambda m: m["body"].update(capability="bash")),
    ("experiment-request", "bad task_type", lambda m: m["body"].update(task_type="explore")),
    ("experiment-request", "nous without max_iterations", lambda m: m["body"].update(limits={})),
    ("experiment-request", "max_iterations zero", lambda m: m["body"].update(limits={"max_iterations": 0})),
    ("experiment-request", "max_iterations bool", lambda m: m["body"].update(limits={"max_iterations": True})),
    ("experiment-request", "unknown body field", lambda m: m["body"].update(approved=True)),
    ("experiment-request", "repeat_of without reason", lambda m: m["body"].update(repeat_of="inv-1")),
    ("experiment-request", "incomplete next_step", lambda m: m["body"].update(
        next_step={"rationale": "x"})),
    ("experiment-request", "locks not object", lambda m: m["body"].update(locks=[])),
    ("receipt", "no reply_to", lambda m: m.pop("reply_to")),
    ("receipt", "bad status", lambda m: m["body"].update(status="ok")),
    ("receipt", "conflict without reasons", lambda m: m["body"].update(status="conflict")),
    ("receipt", "bad timestamp", lambda m: m["body"].update(received_at="yesterday")),
    ("progress", "bad state", lambda m: m["body"].update(state="done")),
    ("progress", "missing at", lambda m: m["body"].pop("at")),
    ("clarification", "bad kind", lambda m: m["body"].update(kind="note")),
    ("clarification", "answer without reply_to", lambda m: m["body"].update(kind="answer")),
    ("cancel", "missing reason", lambda m: m["body"].pop("reason")),
    ("cancel", "interrupt not bool", lambda m: m["body"].update(interrupt="yes")),
    ("experiment-result", "findings on failure", lambda m: m["body"]["execution"].update(state="failed")),
    ("experiment-result", "unavailable without explanation", lambda m: m["body"]["outcome"].update(
        classification="unavailable", explanation=None)),
    ("experiment-result", "unknown spend reported as zero", lambda m: m["body"]["spend"].update(
        completeness="unknown", reported_usd=0)),
    ("experiment-result", "bad enforced_by", lambda m: m["body"]["limits"][0].update(enforced_by="hope")),
    ("experiment-result", "missing versions", lambda m: m["body"].pop("versions")),
    ("experiment-result", "missing collection", lambda m: m["body"].pop("collection")),
    ("experiment-result", "findings after failed collection", lambda m: m["body"].update(
        collection={"state": "failed", "reason": "disk"})),
    ("experiment-result", "collection failed without reason", lambda m: m["body"].update(
        collection={"state": "failed", "reason": None})),
    ("experiment-result", "verdict on a failed execution", lambda m: (
        m["body"].pop("findings"), m["body"]["execution"].update(state="failed"))),
    ("experiment-result", "verdict without hypothesis_ref", lambda m: m["body"]["outcome"].update(
        hypothesis_ref=None)),
    ("experiment-result", "passed but validator exit 1", lambda m: m["body"]["validation"].update(
        exit_code=1)),
    ("experiment-result", "draft-only claim key", lambda m: m["body"]["findings"]["claims"][0].update(
        verified=True)),
    ("experiment-result", "estimate on a candidate", lambda m: m["body"]["findings"].update(
        candidates=[{"id": "cand-x-0001", "estimated_impact": "high"}])),
    ("experiment-result", "bad classified_by", lambda m: m["body"]["outcome"].update(
        classified_by="agent")),
]

ENVELOPE_MALFORMED = [
    ("missing message_id", lambda m: m.pop("message_id")),
    ("missing work_id", lambda m: m.pop("work_id")),
    ("revision zero", lambda m: m.update(revision=0)),
    ("revision string", lambda m: m.update(revision="1")),
    ("unknown envelope field", lambda m: m.update(extra=1)),
    ("short commit", lambda m: m["target"].update(commit="abc123")),
    ("uppercase commit", lambda m: m["target"].update(commit="A" * 40)),
    ("missing repo_name", lambda m: m["target"].pop("repo_name")),
    ("id with slash", lambda m: m.update(message_id="../etc")),
    ("artifacts not list", lambda m: m.update(artifacts={})),
    ("artifact without digest", lambda m: m.update(artifacts=[
        {"kind": "log", "location": "/tmp/x", "provisional": False}])),
    ("body missing", lambda m: m.pop("body")),
    ("body not object", lambda m: m.update(body=[])),
]


class MessageTypes(unittest.TestCase):
    def test_every_type_has_a_valid_case(self):
        self.assertEqual(set(VALID), set(SCHEMA_VERSIONS))
        for t, msg in VALID.items():
            with self.subTest(t):
                self.assertEqual(validate_message(msg, NOENV), [])

    def test_every_type_has_malformed_cases(self):
        self.assertEqual({t for t, _, _ in MALFORMED}, set(SCHEMA_VERSIONS))

    def test_malformed_bodies_are_rejected(self):
        for t, desc, fn in MALFORMED:
            with self.subTest(f"{t}: {desc}"):
                errs = validate_message(mutate(VALID[t], fn), NOENV)
                self.assertTrue(errs, desc)

    def test_malformed_envelopes_are_rejected_for_every_type(self):
        for t, msg in VALID.items():
            for desc, fn in ENVELOPE_MALFORMED:
                with self.subTest(f"{t}: {desc}"):
                    self.assertTrue(validate_message(mutate(msg, fn), NOENV))

    def test_check_message_raises_with_all_errors(self):
        bad = mutate(VALID["cancel"], lambda m: (m.pop("work_id"), m["body"].pop("reason")))
        with self.assertRaises(ContractError) as cm:
            check_message(bad, NOENV)
        self.assertGreaterEqual(len(cm.exception.errors), 2)

    def test_non_object_top_level(self):
        self.assertEqual(validate_message([], NOENV), ["top level: must be an object"])

    def test_required_fields_never_inferred(self):
        m = mutate(VALID["experiment-request"], lambda m: m["body"].pop("task_type"))
        errs = validate_message(m, NOENV)
        self.assertIn("$.body.task_type: required", errs)
        self.assertNotIn("task_type", m["body"])  # not filled in


class SchemaVersions(unittest.TestCase):
    def test_unsupported_version_rejected(self):
        for t, msg in VALID.items():
            for schema in (f"ains.{t}/v2", f"ains.{t}/v0", f"ains.{t}/v10"):
                with self.subTest(schema):
                    errs = validate_message(mutate(msg, lambda m: m.update(schema=schema)), NOENV)
                    self.assertTrue(any("unsupported version" in e for e in errs), errs)

    def test_malformed_schema_ids(self):
        for s in ("experiment-request", "ains.experiment-request", "ains.nope/v1",
                  "AINS.cancel/v1", "ains.cancel/1", None, 1):
            with self.subTest(s):
                with self.assertRaises(ContractError):
                    parse_schema(s)

    def test_parse_schema(self):
        self.assertEqual(parse_schema("ains.cancel/v1"), ("cancel", 1))


class ArtifactRefs(unittest.TestCase):
    def test_rules(self):
        ok = {"kind": "engine-log", "location": "/abs/ok.log", "provisional": True}
        self.assertEqual(validate_artifact_ref(ok), [])
        self.assertEqual(validate_artifact_ref({**ok, "provisional": False, "digest": DIGEST}), [])
        self.assertTrue(validate_artifact_ref({**ok, "provisional": False}))
        self.assertTrue(validate_artifact_ref({**ok, "kind": "/abs/path"}))
        self.assertTrue(validate_artifact_ref({**ok, "digest": "md5:abc"}))
        self.assertTrue(validate_artifact_ref({**ok, "provisional": "no"}))
        self.assertTrue(validate_artifact_ref({**ok, "extra": 1}))

    def test_identity_is_digest_not_path(self):
        from ains_runtime.artifacts import identity, make_ref
        a = make_ref("x", "/one/place", digest=DIGEST)
        b = make_ref("x", "/other/place", digest=DIGEST)
        self.assertEqual(identity(a), identity(b))
        with self.assertRaises(ValueError):
            identity(make_ref("x", "/log", provisional=True))


class SchemaDocs(unittest.TestCase):
    def test_every_type_has_a_schema_doc(self):
        for t in list(SCHEMA_VERSIONS) + ["envelope"]:
            with self.subTest(t):
                doc = json.loads((SCHEMAS / f"{t}.v1.json").read_text())
                self.assertIn("contracts.py", doc["description"])

    def test_examples_validate(self):
        files = sorted(EXAMPLES.glob("*.json"))
        self.assertTrue(files)
        seen = set()
        for f in files:
            with self.subTest(f.name):
                msg = json.loads(f.read_text())
                self.assertEqual(validate_message(msg, NOENV), [])
                seen.add(parse_schema(msg["schema"])[0])
        self.assertEqual(seen, set(SCHEMA_VERSIONS))

    def test_schema_docs_point_at_existing_examples(self):
        for t in SCHEMA_VERSIONS:
            doc = json.loads((SCHEMAS / f"{t}.v1.json").read_text())
            for rel in doc.get("x-examples", []):
                self.assertTrue((SCHEMAS / rel).is_file(), rel)


if __name__ == "__main__":
    unittest.main()
