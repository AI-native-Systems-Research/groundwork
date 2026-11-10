"""Reviewable JSON export of a study's runtime DB into $STUDY/runtime/.

Layout (plan §3.2):
  $STUDY/runtime/index.json
  $STUDY/runtime/requests/<message_id>.json      submitted messages + receipts
  $STUDY/runtime/preparations/<prep_id>.json
  $STUDY/runtime/approvals/<approval_id>.json
  $STUDY/runtime/invocations/<inv_id>.json
  $STUDY/runtime/results/<inv_id>.json           latest experiment-result per
                                                 invocation (`runtime.py result`)

Every file is a REVIEW COPY: it carries `"grants": "nothing"`, and no code
path reads these files back into a store (spec §6). `export` rewrites the
managed subdirectories from the DB, so a hand edit there is overwritten,
never trusted. Records are secret-checked before anything is written.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Mapping

from . import EXPORT_SCHEMA, RUNTIME_VERSION
from .canonical import pretty_json
from .envpolicy import find_secret_values
from .store import Store, StoreError

SUBDIRS = ("requests", "preparations", "approvals", "invocations", "results")
NOTICE = ("Review copy exported from the runtime DB. Copying it grants no "
          "permission to execute or replay anything; launches are authorized only "
          "by the DB under the study's runs directory.")


def _wrap(kind: str, record: dict) -> dict:
    return {"export_schema": EXPORT_SCHEMA, "kind": kind, "grants": "nothing",
            "notice": NOTICE, "record": record}


def collect_records(store: Store) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {d: {} for d in SUBDIRS}
    for m in store.list_messages():
        out["requests"][m["message_id"]] = _wrap("message", {
            "message": m["envelope"], "receipt": m["receipt"],
            "payload_digest": m["payload_digest"], "received_at": m["received_at"]})
    for p in store.list_preparations():
        out["preparations"][p["prep_id"]] = _wrap("preparation", {
            "prep_id": p["prep_id"], "work_id": p["work_id"],
            "revision": p["revision"], "material_digest": p["material_digest"],
            "created_at": p["created_at"], "preparation": p["record"]})
    for a in store.list_approvals():
        out["approvals"][a["approval_id"]] = _wrap("approval", dict(a))
    for i in store.list_invocations():
        out["invocations"][i["inv_id"]] = _wrap("invocation", {
            **i, "events": store.list_events(entity_id=i["inv_id"])})
    from .results import latest_results
    for inv_id, r in latest_results(store).items():
        out["results"][inv_id] = _wrap("experiment-result", r)
    return out


def export_study(store: Store, study: Path,
                 env: Mapping[str, str] | None = None) -> dict:
    """Rewrite study/runtime/ from the DB. Returns the index written."""
    records = collect_records(store)
    index = {"export_schema": EXPORT_SCHEMA, "grants": "nothing", "notice": NOTICE,
             "runtime_version": RUNTIME_VERSION,
             "store_schema_version": store.schema_version(),
             "work": store.list_work(),
             "counts": {d: len(records[d]) for d in SUBDIRS}}
    found = find_secret_values({"index": index, "records": records}, env)
    if found:
        raise StoreError("export refused: " + "; ".join(found))

    root = Path(study) / "runtime"
    root.mkdir(parents=True, exist_ok=True)
    for sub in SUBDIRS:
        # Write each subdir to a temp sibling, then swap, so a crash never
        # leaves a half-written mix of old and new records.
        tmp = Path(tempfile.mkdtemp(prefix=f".{sub}.", dir=root))
        for rid, rec in sorted(records[sub].items()):
            (tmp / f"{rid}.json").write_text(pretty_json(rec), encoding="utf-8")
        dest = root / sub
        old = None
        if dest.exists():
            old = root / f".{sub}.old"
            if old.exists():
                shutil.rmtree(old)
            os.replace(dest, old)
        os.replace(tmp, dest)
        if old is not None:
            shutil.rmtree(old)
    (root / "index.json").write_text(pretty_json(index), encoding="utf-8")
    return index
