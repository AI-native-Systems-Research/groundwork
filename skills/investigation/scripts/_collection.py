"""Shared merge into <study>/collection.json (collection_version 2). Stdlib only.

collection.json is one file with a top-level key per method:

  {"collection_version": 2,
   "nous":       <collect.py bundle, the v1 top-level shape unchanged>,
   "telemetry":  {<capture-id>: <collect_telemetry.py section>, ...},
   "spotlights": <collect_spotlights.py section>}

Each collector replaces only its own key (for telemetry: only its own capture
id inside "telemetry") and keeps every other top-level key as it found it.

Legacy: a v1 collection.json (no collection_version, the bare collect.py
bundle at top level) is read as {"nous": <that bundle>}, so the old Nous
result survives a telemetry/spotlights merge and is replaced by a nous merge.

A collection.json that exists but isn't a JSON object is an error
(CollectionError); it is never silently overwritten. The write goes through a
temp file in the study dir and os.replace, so a crash leaves the old file.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Callable

COLLECTION_VERSION = 2
COLLECTION_FILE = "collection.json"

# Keys only a v1 (bare collect.py bundle) collection.json has at top level.
_V1_MARKERS = ("guards", "claims_draft", "usable")


class CollectionError(Exception):
    """collection.json exists but can't be read as a JSON object."""


def read_collection(study: Path) -> dict:
    """The current collection, normalized to the v2 shape ({} if absent)."""
    path = Path(study) / COLLECTION_FILE
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise CollectionError(f"{path} is unreadable: {e}") from e
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise CollectionError(f"{path} is not valid JSON ({e}); refusing to overwrite it") from e
    if not isinstance(doc, dict):
        raise CollectionError(f"{path} is not a JSON object; refusing to overwrite it")
    if "collection_version" not in doc and all(k in doc for k in _V1_MARKERS):
        return {"nous": doc}
    return doc


def update_collection(study: Path, mutate: Callable[[dict], None]) -> Path:
    """Read collection.json, let `mutate` change it in place, write it back
    atomically with collection_version set. Returns the path written."""
    study = Path(study)
    doc = read_collection(study)
    mutate(doc)
    doc["collection_version"] = COLLECTION_VERSION
    ordered = {"collection_version": COLLECTION_VERSION}
    ordered.update((k, v) for k, v in doc.items() if k != "collection_version")
    text = json.dumps(ordered, indent=2) + "\n"
    path = study / COLLECTION_FILE
    fd, tmp = tempfile.mkstemp(prefix=".collection.", suffix=".tmp", dir=study)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def merge_section(study: Path, key: str, value) -> Path:
    """Replace the top-level `key` with `value`, keeping every other key."""
    def _m(doc: dict) -> None:
        doc[key] = value
    return update_collection(study, _m)


def merge_subsection(study: Path, key: str, subkey: str, value) -> Path:
    """Set doc[key][subkey] = value, keeping other subkeys (e.g. other
    telemetry capture ids) and every other top-level key."""
    def _m(doc: dict) -> None:
        cur = doc.get(key)
        if not isinstance(cur, dict):
            cur = {}
        cur[subkey] = value
        doc[key] = cur
    return update_collection(study, _m)
