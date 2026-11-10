"""Canonical JSON and sha256 digests.

canonical_json(obj) is the one serialization every digest is taken over:
sorted keys, no insignificant whitespace, UTF-8 (non-ASCII kept as-is, never
\\u-escaped), no NaN/Infinity, string keys only. Two JSON texts that parse to
the same value give the same canonical bytes whatever their key order or
escaping. Strings are NOT Unicode-normalized: NFC "é" and NFD "e\\u0301" are
different content and get different digests (normalizing would silently
equate inputs the engines may treat differently).

loads_strict(text) parses JSON and rejects duplicate keys and the
non-standard NaN/Infinity constants, so an ambiguous input can't be
digested one way and read another.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

DIGEST_PREFIX = "sha256:"
SHORT_CODE_LEN = 12


class CanonicalError(ValueError):
    """A value can't be represented as canonical JSON."""


def _check(obj: Any, path: str) -> None:
    if obj is None or isinstance(obj, (bool, str)):
        return
    if isinstance(obj, int):
        return
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise CanonicalError(f"{path}: non-finite number")
        return
    if isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _check(v, f"{path}[{i}]")
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise CanonicalError(f"{path}: non-string key {k!r}")
            _check(v, f"{path}.{k}")
        return
    raise CanonicalError(f"{path}: unsupported type {type(obj).__name__}")


def canonical_json(obj: Any) -> str:
    _check(obj, "$")
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def canonical_bytes(obj: Any) -> bytes:
    return canonical_json(obj).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(obj: Any) -> str:
    """'sha256:<hex>' of the canonical JSON of obj."""
    return DIGEST_PREFIX + sha256_hex(canonical_bytes(obj))


def material_digest(material: dict) -> str:
    """Digest of an adapter's material record (plan §4.2)."""
    if not isinstance(material, dict):
        raise CanonicalError("material must be a mapping")
    return digest(material)


def short_code(digest_str: str) -> str:
    """The 12-hex short code an owner quotes in chat (plan §1 Q1)."""
    if not digest_str.startswith(DIGEST_PREFIX):
        raise CanonicalError("not a sha256: digest")
    return digest_str[len(DIGEST_PREFIX):][:SHORT_CODE_LEN]


def _reject_constant(name: str):
    raise CanonicalError(f"non-standard JSON constant {name}")


def _pairs(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise CanonicalError(f"duplicate key {k!r}")
        out[k] = v
    return out


def loads_strict(text: str) -> Any:
    """json.loads that rejects duplicate keys and NaN/Infinity."""
    try:
        return json.loads(text, object_pairs_hook=_pairs,
                          parse_constant=_reject_constant)
    except json.JSONDecodeError as e:
        raise CanonicalError(f"invalid JSON: {e}") from e


def pretty_json(obj: Any) -> str:
    """Stable human-readable JSON (exports, show --json). Not for digests."""
    _check(obj, "$")
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False,
                      allow_nan=False) + "\n"
