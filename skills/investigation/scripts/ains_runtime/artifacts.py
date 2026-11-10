"""Artifact references: {kind, location, digest?, provisional}.

spec §6: an artifact reference carries a kind, a location and a content
digest for immutable artifacts. A local absolute path is allowed as a
location but never as an identity, and mutable things (logs of a running
invocation) are referenced as provisional until finalized.

Rules:
  kind         non-empty token ([a-z0-9][a-z0-9._-]*), e.g. "campaign-yaml",
               "engine-log", "answer-json". Not a path.
  location     non-empty string (relative or absolute path, or URI).
  digest       "sha256:<64 lowercase hex>"; required unless provisional.
  provisional  bool, required. A provisional ref may omit the digest, or carry
               one that is only a snapshot.
No other keys.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .canonical import DIGEST_PREFIX

KIND_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REF_KEYS = {"kind", "location", "digest", "provisional"}


def validate_artifact_ref(ref: Any, path: str = "artifact") -> list[str]:
    if not isinstance(ref, dict):
        return [f"{path}: must be an object"]
    errs = []
    for k in sorted(set(ref) - REF_KEYS):
        errs.append(f"{path}.{k}: unknown field")
    kind = ref.get("kind")
    if not isinstance(kind, str) or not KIND_RE.match(kind):
        errs.append(f"{path}.kind: required token matching {KIND_RE.pattern}")
    loc = ref.get("location")
    if not isinstance(loc, str) or not loc.strip():
        errs.append(f"{path}.location: required non-empty string")
    prov = ref.get("provisional")
    if not isinstance(prov, bool):
        errs.append(f"{path}.provisional: required boolean")
    if "digest" in ref:
        d = ref["digest"]
        if not isinstance(d, str) or not DIGEST_RE.match(d):
            errs.append(f"{path}.digest: must be sha256:<64 lowercase hex>")
    elif prov is not True:
        errs.append(f"{path}.digest: required unless provisional is true")
    return errs


def file_digest(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return DIGEST_PREFIX + h.hexdigest()


def make_ref(kind: str, location: str | Path, *, digest: str | None = None,
             provisional: bool = False) -> dict:
    """Build a ref. For a non-provisional ref with no digest, hash the file."""
    loc = str(location)
    ref: dict = {"kind": kind, "location": loc, "provisional": provisional}
    if digest is None and not provisional:
        digest = file_digest(loc)
    if digest is not None:
        ref["digest"] = digest
    errs = validate_artifact_ref(ref)
    if errs:
        raise ValueError("; ".join(errs))
    return ref


def identity(ref: dict) -> str:
    """Portable identity of a ref: its digest. Provisional refs have none."""
    if ref.get("provisional") or "digest" not in ref:
        raise ValueError("a provisional artifact has no portable identity")
    return ref["digest"]
