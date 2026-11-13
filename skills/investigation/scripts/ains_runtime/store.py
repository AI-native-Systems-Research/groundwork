"""SQLite store (plan §3.3): the authority for execution bookkeeping.

One DB per study, at $RUNS/<repo>/<slug>/.runtime/runtime.sqlite3, in WAL
mode. Schema v1 tables:

  meta          single row: schema_version, runtime_version, created_at
  messages      one row per accepted message id (payload digest, envelope,
                the receipt that was returned)
  work          one row per work id: revision, state, capability
  preparations  immutable prepared-invocation records (R2 writes them)
  approvals     single-use approvals bound to one preparation's digest (R2)
  invocations   reservations and execution identity (R3); a partial unique
                index on material_digest stops two equivalent invocations
                unless one is an explicit repeat
  events        append-only lifecycle log (UPDATE/DELETE are refused by
                triggers); every write path checks the detail for secret
                values when given env and refuses the transaction otherwise

Every state change runs inside `with store.tx():` which issues BEGIN
IMMEDIATE, so two writers serialize before reading the state they act on.

Nothing here imports state from outside the DB: there is no import path for
exported JSON. A preparation, approval or invocation exists only if it was
written through this API into this DB, so copying a study's exported
records into another checkout grants nothing (spec §6).

Public API used by later phases:
  open_store(path) -> Store            create/upgrade/verify the DB
  Store.tx()                           BEGIN IMMEDIATE context manager
  Store.submit(msg, env=None)          -> SubmitResult (idempotent, R1)
  Store.insert_preparation(...)        R2 prepare
  Store.insert_approval(...)           R2 approve (short-code check is approval.py's)
  Store.reserve_invocation(...)        R3 launch: approval check + consume +
                                       duplicate check + insert, one transaction
  Store.set_invocation_state(...)      R3 supervisor/recovery
  Store.add_event(...), get_*/list_*   reads for show/export/status
  Store.get_invocation_for_prep(id)    R2 revalidation (one invocation per prep)
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from . import RUNTIME_VERSION, STORE_SCHEMA_VERSION
from .canonical import canonical_json, digest, loads_strict, material_digest, short_code
from .contracts import (ContractError, ID_RE, check_message, message_type,
                        schema_id, validate_message)
from .envpolicy import find_secret_values

DB_NAME = "runtime.sqlite3"


class StoreError(Exception):
    """The DB can't be opened, has an unsupported schema, or refused a write."""


class GrantError(StoreError):
    """A launch reservation was refused (no preparation/approval, used, duplicate)."""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


SCHEMA_V1 = """
CREATE TABLE meta (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  schema_version INTEGER NOT NULL,
  runtime_version TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE messages (
  message_id TEXT PRIMARY KEY,
  payload_digest TEXT NOT NULL,
  type TEXT NOT NULL,
  work_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  received_at TEXT NOT NULL,
  envelope_json TEXT NOT NULL,
  receipt_json TEXT NOT NULL
);
CREATE INDEX messages_work ON messages(work_id);
CREATE TABLE work (
  work_id TEXT PRIMARY KEY,
  revision INTEGER NOT NULL,
  state TEXT NOT NULL,
  capability TEXT,
  repo_name TEXT NOT NULL,
  request_message_id TEXT NOT NULL REFERENCES messages(message_id),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE preparations (
  prep_id TEXT PRIMARY KEY,
  work_id TEXT NOT NULL REFERENCES work(work_id),
  revision INTEGER NOT NULL,
  material_digest TEXT NOT NULL,
  record_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE approvals (
  approval_id TEXT PRIMARY KEY,
  prep_id TEXT NOT NULL UNIQUE REFERENCES preparations(prep_id),
  material_digest TEXT NOT NULL,
  source TEXT NOT NULL,
  owner_quote TEXT NOT NULL,
  approved_at TEXT NOT NULL,
  consumed_at TEXT
);
CREATE TABLE invocations (
  inv_id TEXT PRIMARY KEY,
  prep_id TEXT NOT NULL UNIQUE REFERENCES preparations(prep_id),
  work_id TEXT NOT NULL REFERENCES work(work_id),
  capability TEXT,
  material_digest TEXT NOT NULL,
  repeat_of TEXT REFERENCES invocations(inv_id),
  state TEXT NOT NULL,
  launcher_pid INTEGER,
  launcher_start TEXT,
  supervisor_pid INTEGER,
  supervisor_start TEXT,
  engine_pid INTEGER,
  engine_start TEXT,
  argv_digest TEXT,
  native_run_id TEXT,
  launch_json TEXT,
  exit_code INTEGER,
  native_json TEXT,
  cancel_requested_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  ended_at TEXT
);
CREATE UNIQUE INDEX invocations_material_once ON invocations(material_digest)
  WHERE repeat_of IS NULL AND state NOT IN ('rejected', 'not_launched');
CREATE TABLE events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  entity TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  work_id TEXT,
  event TEXT NOT NULL,
  detail_json TEXT NOT NULL
);
CREATE INDEX events_work ON events(work_id);
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
  BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
  BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""

def _statements(script: str) -> list[str]:
    """Split a schema script into statements; a CREATE TRIGGER ... END; is one.

    (executescript() would COMMIT the open BEGIN IMMEDIATE, so it is not used.)
    """
    out, buf = [], []
    for line in script.strip().splitlines():
        buf.append(line)
        text = "\n".join(buf).strip()
        in_trigger = text.upper().startswith("CREATE TRIGGER")
        if text.endswith(";") and (not in_trigger or text.upper().endswith("END;")):
            if sqlite3.complete_statement(text):
                out.append(text)
                buf = []
    if "\n".join(buf).strip():
        raise StoreError("schema script has a trailing incomplete statement")
    return out


# Migration hook: MIGRATIONS[n] upgrades a DB at schema n to n+1, inside the
# open transaction. Empty at v1. A DB newer than STORE_SCHEMA_VERSION is
# refused (an older runtime must not write a newer schema).
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {}


@dataclass
class SubmitResult:
    status: str                 # accepted | duplicate | rejected | conflict
    receipt: dict | None        # receipt envelope (None when unidentifiable)
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("accepted", "duplicate")

    def output(self) -> dict:
        if self.receipt is not None:
            return self.receipt
        return {"status": self.status, "reasons": self.reasons}


def _no_secrets(obj: Any, what: str, env: Mapping[str, str] | None) -> None:
    found = find_secret_values(obj, env)
    if found:
        raise StoreError(f"{what} refused: " + "; ".join(found))


class Store:
    def __init__(self, conn: sqlite3.Connection, path: Path):
        self.conn = conn
        self.path = path

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE ... COMMIT (ROLLBACK on any exception)."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # --- meta ---------------------------------------------------------------

    def schema_version(self) -> int:
        return self.conn.execute("SELECT schema_version FROM meta").fetchone()[0]

    def meta(self) -> dict:
        r = self.conn.execute("SELECT * FROM meta").fetchone()
        return dict(r)

    # --- events -------------------------------------------------------------

    def add_event(self, entity: str, entity_id: str, event: str, *,
                  work_id: str | None = None, detail: dict | None = None,
                  at: str | None = None, env: Mapping[str, str] | None = None) -> None:
        """Append one event. Call inside tx().

        The detail must be secret-free when `env` is given: like every other
        write (messages, preparations, approvals, invocation_event), a secret
        value in an event is refused with StoreError so the transaction rolls
        back — never redacted-and-kept, which would make the append-only
        events log the one table a leaked value can never be scrubbed from.
        """
        if env is not None:
            _no_secrets(detail or {}, f"event {event}", env)
        self.conn.execute(
            "INSERT INTO events(at, entity, entity_id, work_id, event, detail_json)"
            " VALUES (?,?,?,?,?,?)",
            (at or now(), entity, entity_id, work_id, event,
             canonical_json(detail or {})))

    def list_events(self, *, work_id: str | None = None,
                    entity_id: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM events", []
        if work_id is not None:
            q, args = q + " WHERE work_id = ?", [work_id]
        elif entity_id is not None:
            q, args = q + " WHERE entity_id = ?", [entity_id]
        rows = self.conn.execute(q + " ORDER BY seq", args).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail_json"])} for r in rows]

    # --- messages / submit --------------------------------------------------

    def _receipt(self, msg: dict, pdigest: str, status: str, work_state: str | None,
                 reasons: list[str], at: str, stored: bool) -> dict:
        h = digest(msg["message_id"])[7:23]
        rid = f"rcpt-{h}" if stored else f"rcpt-{h}-{status}"
        receipt = {
            "schema": schema_id("receipt"),
            "message_id": rid,
            "work_id": msg["work_id"],
            "revision": msg["revision"],
            "reply_to": msg["message_id"],
            "target": msg["target"],
            "artifacts": [],
            "body": {
                "received_message_id": msg["message_id"],
                "received_schema": msg["schema"],
                "payload_digest": pdigest,
                "status": "accepted" if status == "duplicate" else status,
                "work_state": work_state,
                "reasons": reasons,
                "received_at": at,
            },
        }
        return check_message(receipt, env={})

    def submit(self, msg: Any, env: Mapping[str, str] | None = None) -> SubmitResult:
        """Validate and record a message. Idempotent on (message_id, payload).

        - invalid message                 -> rejected, nothing stored
        - known id, same payload digest   -> duplicate, the stored receipt
        - known id, different digest      -> conflict, nothing stored
        - valid but not applicable (stale revision, unknown work, ...)
                                          -> rejected, nothing stored
        - otherwise                       -> accepted, message + work + event
                                             + receipt stored in one transaction
        """
        errs = validate_message(msg, env)
        identifiable = (isinstance(msg, dict)
                        and not any(e.startswith(("$.message_id", "$.work_id",
                                                  "$.revision", "$.target",
                                                  "schema", "top level"))
                                    for e in errs)
                        and not find_secret_values(msg, env))
        if errs:
            if not identifiable:
                return SubmitResult("rejected", None, errs)
            pd = digest(msg)
            return SubmitResult("rejected", self._receipt(
                msg, pd, "rejected", None, errs, now(), stored=False), errs)
        mtype = message_type(msg)
        from .contracts import SUBMITTABLE_TYPES
        pdigest = digest(msg)
        if mtype not in SUBMITTABLE_TYPES:
            reasons = [f"schema: {mtype} messages are written by the runtime, not submitted"]
            return SubmitResult("rejected", self._receipt(
                msg, pdigest, "rejected", None, reasons, now(), stored=False), reasons)

        with self.tx() as c:
            row = c.execute("SELECT payload_digest, receipt_json FROM messages"
                            " WHERE message_id = ?", (msg["message_id"],)).fetchone()
            if row is not None:
                if row["payload_digest"] == pdigest:
                    return SubmitResult("duplicate", json.loads(row["receipt_json"]))
                reasons = [f"message_id {msg['message_id']!r} was already used for a "
                           f"different payload ({row['payload_digest']})"]
                return SubmitResult("conflict", self._receipt(
                    msg, pdigest, "conflict", None, reasons, now(), stored=False),
                    reasons)

            at = now()
            work = c.execute("SELECT * FROM work WHERE work_id = ?",
                             (msg["work_id"],)).fetchone()
            reasons = self._applicability(c, mtype, msg, work)
            if reasons:
                return SubmitResult("rejected", self._receipt(
                    msg, pdigest, "rejected", work["state"] if work else None,
                    reasons, at, stored=False), reasons)

            new_state = self._apply(c, mtype, msg, work, at)
            receipt = self._receipt(msg, pdigest, "accepted", new_state, [], at,
                                    stored=True)
            c.execute("INSERT INTO messages(message_id, payload_digest, type, work_id,"
                      " revision, received_at, envelope_json, receipt_json)"
                      " VALUES (?,?,?,?,?,?,?,?)",
                      (msg["message_id"], pdigest, mtype, msg["work_id"],
                       msg["revision"], at, canonical_json(msg),
                       canonical_json(receipt)))
            if mtype == "experiment-request":
                self._upsert_work(c, msg, work, new_state, at)
            elif work is not None and new_state != work["state"]:
                c.execute("UPDATE work SET state = ?, updated_at = ? WHERE work_id = ?",
                          (new_state, at, msg["work_id"]))
            self.add_event("message", msg["message_id"], f"received:{mtype}",
                           work_id=msg["work_id"], at=at,
                           detail={"payload_digest": pdigest,
                                   "revision": msg["revision"]})
            if work is None or new_state != work["state"]:
                self.add_event("work", msg["work_id"], f"state:{new_state}",
                               work_id=msg["work_id"], at=at,
                               detail={"by_message": msg["message_id"]})
            return SubmitResult("accepted", receipt)

    def _applicability(self, c, mtype: str, msg: dict, work) -> list[str]:
        if work is not None and work["repo_name"] != msg["target"]["repo_name"]:
            return [f"target.repo_name: work {msg['work_id']!r} belongs to "
                    f"{work['repo_name']!r}"]
        if mtype == "experiment-request":
            if work is not None and msg["revision"] <= work["revision"]:
                return [f"revision: {msg['revision']} is not newer than the work's "
                        f"current revision {work['revision']}"]
            if work is None and msg["revision"] != 1:
                return ["revision: a new work item starts at revision 1"]
            rp = msg["body"].get("repeat_of")
            if rp is not None and c.execute(
                    "SELECT 1 FROM invocations WHERE inv_id = ?", (rp,)).fetchone() is None:
                return [f"body.repeat_of: no invocation {rp!r} in this study's runtime DB"]
            return []
        if work is None:
            return [f"work_id: no experiment-request recorded for {msg['work_id']!r}"]
        if msg["revision"] != work["revision"]:
            return [f"revision: {msg['revision']} is not the work's current revision "
                    f"{work['revision']}"]
        if mtype == "clarification" and msg["body"]["kind"] == "answer":
            q = c.execute("SELECT type, envelope_json FROM messages WHERE message_id = ?"
                          " AND work_id = ?", (msg["reply_to"], msg["work_id"])).fetchone()
            if q is None or q["type"] != "clarification" or \
                    json.loads(q["envelope_json"])["body"]["kind"] != "question":
                return [f"reply_to: {msg['reply_to']!r} is not a clarification "
                        "question for this work"]
        if mtype == "cancel" and msg["body"].get("invocation_id") is not None:
            if c.execute("SELECT 1 FROM invocations WHERE inv_id = ? AND work_id = ?",
                         (msg["body"]["invocation_id"], msg["work_id"])).fetchone() is None:
                return [f"body.invocation_id: no invocation "
                        f"{msg['body']['invocation_id']!r} for this work"]
        return []

    @staticmethod
    def _apply(c, mtype: str, msg: dict, work, at: str) -> str:
        if mtype == "experiment-request":
            return "accepted"
        state = work["state"]
        if mtype == "clarification":
            if msg["body"]["kind"] == "question" and state == "accepted":
                return "waiting_for_answer"
            if msg["body"]["kind"] == "answer" and state == "waiting_for_answer":
                return "accepted"
        # cancel: recorded only; R3's `cancel` acts on invocations. A cancel
        # receipt is never a claim that anything stopped (spec §8).
        return state

    @staticmethod
    def _upsert_work(c, msg: dict, work, state: str, at: str) -> None:
        cap = msg["body"]["capability"]
        if work is None:
            c.execute("INSERT INTO work(work_id, revision, state, capability, repo_name,"
                      " request_message_id, created_at, updated_at)"
                      " VALUES (?,?,?,?,?,?,?,?)",
                      (msg["work_id"], msg["revision"], state, cap,
                       msg["target"]["repo_name"], msg["message_id"], at, at))
        else:
            c.execute("UPDATE work SET revision = ?, state = ?, capability = ?,"
                      " request_message_id = ?, updated_at = ? WHERE work_id = ?",
                      (msg["revision"], state, cap, msg["message_id"], at,
                       msg["work_id"]))

    def get_message(self, message_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM messages WHERE message_id = ?",
                              (message_id,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["envelope"] = json.loads(d.pop("envelope_json"))
        d["receipt"] = json.loads(d.pop("receipt_json"))
        return d

    def list_messages(self, work_id: str | None = None) -> list[dict]:
        q, args = "SELECT message_id FROM messages", []
        if work_id is not None:
            q, args = q + " WHERE work_id = ?", [work_id]
        ids = [r[0] for r in self.conn.execute(q + " ORDER BY received_at, message_id",
                                               args)]
        return [self.get_message(i) for i in ids]

    def get_work(self, work_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM work WHERE work_id = ?",
                              (work_id,)).fetchone()
        return dict(r) if r else None

    def list_work(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM work ORDER BY created_at, work_id")]

    # --- preparations / approvals / invocations (written by R2/R3) ----------

    def insert_preparation(self, prep_id: str, work_id: str, record: dict, *,
                           env: Mapping[str, str] | None = None) -> str:
        """Record an immutable preparation. record["material"] is digested.

        Returns the material digest. The work must exist in THIS DB and the
        record must be secret-free. Preparations are never updated.
        """
        if not isinstance(prep_id, str) or not ID_RE.match(prep_id):
            raise StoreError(f"prep_id: must match {ID_RE.pattern}")
        if not isinstance(record, dict) or not isinstance(record.get("material"), dict):
            raise StoreError("preparation record must carry a material mapping")
        _no_secrets(record, "preparation record", env)
        mdig = material_digest(record["material"])
        if record.get("material_digest", mdig) != mdig:
            raise StoreError("preparation record's material_digest does not match "
                             "its material")
        record = {**record, "material_digest": mdig}
        with self.tx() as c:
            work = c.execute("SELECT revision FROM work WHERE work_id = ?",
                             (work_id,)).fetchone()
            if work is None:
                raise StoreError(f"no work {work_id!r} in this runtime DB")
            try:
                c.execute("INSERT INTO preparations(prep_id, work_id, revision,"
                          " material_digest, record_json, created_at)"
                          " VALUES (?,?,?,?,?,?)",
                          (prep_id, work_id, work["revision"], mdig,
                           canonical_json(record), now()))
            except sqlite3.IntegrityError as e:
                raise StoreError(f"preparation {prep_id!r} already exists") from e
            self.add_event("preparation", prep_id, "prepared", work_id=work_id,
                           detail={"material_digest": mdig})
        return mdig

    def get_preparation(self, prep_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM preparations WHERE prep_id = ?",
                              (prep_id,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["record"] = json.loads(d.pop("record_json"))
        return d

    def list_preparations(self, work_id: str | None = None) -> list[dict]:
        q, args = "SELECT prep_id FROM preparations", []
        if work_id is not None:
            q, args = q + " WHERE work_id = ?", [work_id]
        return [self.get_preparation(r[0]) for r in
                self.conn.execute(q + " ORDER BY created_at, prep_id", args)]

    def insert_approval(self, approval_id: str, prep_id: str, material_digest_: str,
                        source: str, owner_quote: str, *,
                        env: Mapping[str, str] | None = None) -> None:
        """Record an approval bound to one preparation's digest.

        The owner-facing checks (short code, quoting) are approval.py's (R2);
        this enforces only that the preparation exists in THIS DB, the digest
        is that preparation's, and the preparation has no approval yet.
        """
        if not isinstance(approval_id, str) or not ID_RE.match(approval_id):
            raise StoreError(f"approval_id: must match {ID_RE.pattern}")
        if not (isinstance(owner_quote, str) and owner_quote.strip()):
            raise StoreError("owner_quote: required")
        if not (isinstance(source, str) and source.strip()):
            raise StoreError("source: required")
        _no_secrets({"source": source, "quote": owner_quote}, "approval", env)
        with self.tx() as c:
            p = c.execute("SELECT work_id, material_digest FROM preparations"
                          " WHERE prep_id = ?", (prep_id,)).fetchone()
            if p is None:
                raise GrantError(f"no preparation {prep_id!r} in this runtime DB")
            if p["material_digest"] != material_digest_:
                raise GrantError("approval digest does not match the preparation's "
                                 "material digest")
            try:
                c.execute("INSERT INTO approvals(approval_id, prep_id, material_digest,"
                          " source, owner_quote, approved_at) VALUES (?,?,?,?,?,?)",
                          (approval_id, prep_id, material_digest_, source,
                           owner_quote, now()))
            except sqlite3.IntegrityError as e:
                raise GrantError(f"preparation {prep_id!r} already has an approval, "
                                 "or approval id is taken") from e
            self.add_event("approval", approval_id, "approved", work_id=p["work_id"],
                           detail={"prep_id": prep_id, "material_digest": material_digest_,
                                   "short_code": short_code(material_digest_),
                                   "source": source})

    def get_approval(self, prep_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM approvals WHERE prep_id = ?",
                              (prep_id,)).fetchone()
        return dict(r) if r else None

    def list_approvals(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM approvals ORDER BY approved_at, approval_id")]

    def reserve_invocation(self, inv_id: str, prep_id: str, *,
                           repeat_of: str | None = None,
                           expected_digest: str | None = None,
                           launcher: tuple[int, str] | None = None,
                           launch: dict | None = None) -> dict:
        """The launch grant (plan §4.3 step 1): ONE transaction that

          1. finds the preparation in THIS DB,
          2. finds its approval, checks the digest and that it is unconsumed,
          3. consumes it,
          4. inserts the invocation as `reserved` — the partial unique index
             refuses an equivalent non-repeat invocation.

        Any failure rolls back everything (the approval stays unconsumed).
        No process is created here; R3's launch spawns after this returns.
        `launcher` is the launching process's (pid, start) identity, so
        recovery can tell "launch still in progress" from "launcher died
        between reserve and spawn". `launch` is the adapter's spawn spec
        (argv, cwd, env names), stored with the reservation so the
        supervisor needs no adapter and spawns exactly what was reserved.
        """
        if not isinstance(inv_id, str) or not ID_RE.match(inv_id):
            raise StoreError(f"inv_id: must match {ID_RE.pattern}")
        if launch is not None:
            _no_secrets(launch, "launch spec", None)
        with self.tx() as c:
            p = c.execute("SELECT * FROM preparations WHERE prep_id = ?",
                          (prep_id,)).fetchone()
            if p is None:
                raise GrantError(f"no preparation {prep_id!r} in this runtime DB; "
                                 "exported records grant nothing")
            mdig = p["material_digest"]
            if expected_digest is not None and expected_digest != mdig:
                raise GrantError("material digest changed since preparation")
            a = c.execute("SELECT * FROM approvals WHERE prep_id = ?",
                          (prep_id,)).fetchone()
            if a is None:
                raise GrantError(f"preparation {prep_id!r} has no approval")
            if a["material_digest"] != mdig:
                raise GrantError("approval is bound to a different material digest")
            if a["consumed_at"] is not None:
                raise GrantError(f"approval for {prep_id!r} was already used")
            if repeat_of is not None and c.execute(
                    "SELECT 1 FROM invocations WHERE inv_id = ?",
                    (repeat_of,)).fetchone() is None:
                raise GrantError(f"repeat_of: no invocation {repeat_of!r}")
            at = now()
            c.execute("UPDATE approvals SET consumed_at = ? WHERE approval_id = ?",
                      (at, a["approval_id"]))
            cap = json.loads(p["record_json"]).get("capability")
            try:
                c.execute("INSERT INTO invocations(inv_id, prep_id, work_id, capability,"
                          " material_digest, repeat_of, state, launcher_pid,"
                          " launcher_start, launch_json, argv_digest, native_run_id,"
                          " created_at, updated_at)"
                          " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (inv_id, prep_id, p["work_id"], cap, mdig, repeat_of,
                           "reserved", launcher[0] if launcher else None,
                           launcher[1] if launcher else None,
                           canonical_json(launch) if launch is not None else None,
                           digest(launch["argv"]) if launch is not None else None,
                           launch.get("native_run_id") if launch is not None else None,
                           at, at))
            except sqlite3.IntegrityError as e:
                raise GrantError("an equivalent invocation already exists "
                                 "(same material digest); use an explicit repeat") from e
            self.add_event("approval", a["approval_id"], "consumed",
                           work_id=p["work_id"], at=at, detail={"inv_id": inv_id})
            self.add_event("invocation", inv_id, "state:reserved", work_id=p["work_id"],
                           at=at, detail={"prep_id": prep_id, "material_digest": mdig,
                                          "repeat_of": repeat_of})
        return self.get_invocation(inv_id)

    INVOCATION_FIELDS = ("launcher_pid", "launcher_start", "supervisor_pid", "supervisor_start", "engine_pid",
                         "engine_start", "argv_digest", "native_run_id", "exit_code",
                         "native_json", "cancel_requested_at", "ended_at")

    def set_invocation_state(self, inv_id: str, state: str, *,
                             detail: dict | None = None, env: Mapping[str, str] | None = None,
                             **fields: Any) -> None:
        """Transition an invocation and record identity fields (R3).

        With `env`, the recorded detail (and fields) must be secret-free
        (see add_event); the transition is refused, not redacted."""
        bad = set(fields) - set(self.INVOCATION_FIELDS)
        if bad:
            raise StoreError(f"unknown invocation fields: {sorted(bad)}")
        with self.tx() as c:
            r = c.execute("SELECT work_id FROM invocations WHERE inv_id = ?",
                          (inv_id,)).fetchone()
            if r is None:
                raise StoreError(f"no invocation {inv_id!r}")
            sets = ", ".join(f"{k} = ?" for k in fields)
            at = now()
            c.execute(f"UPDATE invocations SET state = ?, updated_at = ?"
                      f"{', ' + sets if sets else ''} WHERE inv_id = ?",
                      (state, at, *fields.values(), inv_id))
            self.add_event("invocation", inv_id, f"state:{state}", work_id=r["work_id"],
                           at=at, detail={**(detail or {}), **fields}, env=env)

    def transition(self, inv_id: str, from_states: tuple[str, ...] | str, state: str, *,
                   detail: dict | None = None, where: Mapping[str, Any] | None = None,
                   env: Mapping[str, str] | None = None, **fields: Any) -> bool:
        """Compare-and-set: move inv_id to `state` only if it is in from_states
        (and every column in `where` has the given value). Returns whether
        the transition happened; the event is written only if it did. This
        is how the supervisor, launch, recovery and reconcile avoid acting
        on a state another process already changed.

        With `env`, the recorded detail (and fields) must be secret-free
        (see add_event); the transition is refused, not redacted."""
        if isinstance(from_states, str):
            from_states = (from_states,)
        bad = (set(fields) | set(where or {})) - set(self.INVOCATION_FIELDS)
        if bad:
            raise StoreError(f"unknown invocation fields: {sorted(bad)}")
        with self.tx() as c:
            r = c.execute("SELECT * FROM invocations WHERE inv_id = ?",
                          (inv_id,)).fetchone()
            if r is None:
                raise StoreError(f"no invocation {inv_id!r}")
            if r["state"] not in from_states:
                return False
            if any(r[k] != v for k, v in (where or {}).items()):
                return False
            sets = "".join(f", {k} = ?" for k in fields)
            at = now()
            c.execute(f"UPDATE invocations SET state = ?, updated_at = ?{sets}"
                      " WHERE inv_id = ?", (state, at, *fields.values(), inv_id))
            self.add_event("invocation", inv_id, f"state:{state}", work_id=r["work_id"],
                           at=at, detail={"from": r["state"], **(detail or {}),
                                          **fields}, env=env)
            return True

    def record_if_null(self, inv_id: str, event: str, *,
                       env: Mapping[str, str] | None = None, **fields: Any) -> bool:
        """Set identity fields only where ALL of them are still NULL (no state
        change). Used by launch to record the supervisor it spawned, unless
        the supervisor already recorded itself.

        With `env`, the recorded fields must be secret-free (see add_event);
        the write is refused, not redacted."""
        bad = set(fields) - set(self.INVOCATION_FIELDS)
        if bad or not fields:
            raise StoreError(f"unknown invocation fields: {sorted(bad)}")
        with self.tx() as c:
            r = c.execute("SELECT * FROM invocations WHERE inv_id = ?",
                          (inv_id,)).fetchone()
            if r is None:
                raise StoreError(f"no invocation {inv_id!r}")
            if any(r[k] is not None for k in fields):
                return False
            sets = ", ".join(f"{k} = ?" for k in fields)
            c.execute(f"UPDATE invocations SET {sets}, updated_at = ? WHERE inv_id = ?",
                      (*fields.values(), now(), inv_id))
            self.add_event("invocation", inv_id, event, work_id=r["work_id"],
                           detail=dict(fields), env=env)
            return True

    def invocation_event(self, inv_id: str, event: str, detail: dict | None = None, *,
                         env: Mapping[str, str] | None = None) -> None:
        """Append a non-transition event (launch spec, cancel result, ...)."""
        _no_secrets(detail or {}, f"event {event}", env)
        with self.tx() as c:
            r = c.execute("SELECT work_id FROM invocations WHERE inv_id = ?",
                          (inv_id,)).fetchone()
            if r is None:
                raise StoreError(f"no invocation {inv_id!r}")
            self.add_event("invocation", inv_id, event, work_id=r["work_id"],
                           detail=detail)

    def request_cancel(self, inv_id: str, detail: dict | None = None, *,
                       env: Mapping[str, str] | None = None) -> bool:
        """Record a cancel request (first one sets cancel_requested_at).
        Returns True if this call set it. Never a claim that anything stopped.

        With `env`, the detail must be secret-free (see add_event): the
        request is refused, and cancel_requested_at stays unset."""
        with self.tx() as c:
            r = c.execute("SELECT work_id, cancel_requested_at FROM invocations"
                          " WHERE inv_id = ?", (inv_id,)).fetchone()
            if r is None:
                raise StoreError(f"no invocation {inv_id!r}")
            at = now()
            first = r["cancel_requested_at"] is None
            if first:
                c.execute("UPDATE invocations SET cancel_requested_at = ?, updated_at = ?"
                          " WHERE inv_id = ?", (at, at, inv_id))
            self.add_event("invocation", inv_id, "cancel:requested", work_id=r["work_id"],
                           at=at, detail={**(detail or {}), "first": first}, env=env)
            return first

    def get_invocation(self, inv_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM invocations WHERE inv_id = ?",
                              (inv_id,)).fetchone()
        return _inv(r)

    def get_invocation_for_prep(self, prep_id: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM invocations WHERE prep_id = ?",
                              (prep_id,)).fetchone()
        return _inv(r)

    def list_invocations(self, work_id: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM invocations", []
        if work_id is not None:
            q, args = q + " WHERE work_id = ?", [work_id]
        return [_inv(r) for r in self.conn.execute(q + " ORDER BY created_at, inv_id",
                                                   args)]

    def count_invocations(self, capability: str | None = None) -> int:
        """Per-study (optionally per-capability) count for approval cards (D8)."""
        if capability is None:
            return self.conn.execute("SELECT COUNT(*) FROM invocations").fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM invocations WHERE capability = ?",
                                 (capability,)).fetchone()[0]


def _inv(r) -> dict | None:
    """An invocation row with its JSON columns decoded (launch, native)."""
    if r is None:
        return None
    d = dict(r)
    for col, key in (("launch_json", "launch"), ("native_json", "native")):
        raw = d.pop(col, None)
        d[key] = json.loads(raw) if raw else None
    return d


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        conn.close()
        raise StoreError(f"{path}: could not enable WAL mode (got {mode})")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def open_store(path: str | Path, *, create: bool = True) -> Store:
    """Open (and create or migrate) the runtime DB at path."""
    path = Path(path)
    if not path.exists():
        if not create:
            raise StoreError(f"{path}: no runtime DB")
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = _connect(path)
    except sqlite3.Error as e:
        raise StoreError(f"{path}: {e}") from e
    store = Store(conn, path)
    try:
        with store.tx() as c:
            has_meta = c.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                                 " AND name='meta'").fetchone()
            if not has_meta:
                if c.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]:
                    raise StoreError(f"{path}: not an AINS runtime DB (no meta table)")
                for stmt in _statements(SCHEMA_V1):
                    c.execute(stmt)
                c.execute("INSERT INTO meta(id, schema_version, runtime_version,"
                          " created_at) VALUES (1, ?, ?, ?)",
                          (STORE_SCHEMA_VERSION, RUNTIME_VERSION, now()))
            else:
                v = c.execute("SELECT schema_version FROM meta").fetchone()[0]
                if v > STORE_SCHEMA_VERSION:
                    raise StoreError(f"{path}: schema v{v} is newer than this runtime "
                                     f"supports (v{STORE_SCHEMA_VERSION}); upgrade the skill")
                while v < STORE_SCHEMA_VERSION:
                    if v not in MIGRATIONS:
                        raise StoreError(f"{path}: no migration from schema v{v}")
                    MIGRATIONS[v](c)
                    v += 1
                    c.execute("UPDATE meta SET schema_version = ?, runtime_version = ?",
                              (v, RUNTIME_VERSION))
    except BaseException:
        conn.close()
        raise
    return store


def load_message_file(path: str | Path) -> Any:
    """Read a message file (or '-' for stdin) with strict JSON parsing."""
    import sys
    text = sys.stdin.read() if str(path) == "-" else Path(path).read_text(encoding="utf-8")
    return loads_strict(text)
