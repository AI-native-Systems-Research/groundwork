"""The locked upstreams (Nous, Spotlights, BLIS) and the facts the skill relies on.

Stdlib only (doctor imports it). Everything the skill knows about another
project lives in references/upstreams/:

  lock.json         which version of each upstream is used (the only place
                    versions live)
  <name>.json       facts about that upstream at the locked version, each
                    with a source citation, plus `verified_against`: the
                    lock id they were checked against

A lock id is `<name>@<12 hex>`, a digest of that upstream's lock entry, so
any edit to the entry changes it. A facts file whose `verified_against`
differs from the current lock id is stale: `facts()` refuses it, and so do
the tests. To bump an upstream, see references/upstreams/README.md.
"""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path

UPSTREAMS_DIR = Path(__file__).resolve().parent.parent / "references" / "upstreams"
LOCK_FILE = UPSTREAMS_DIR / "lock.json"


class UpstreamError(Exception):
    pass


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise UpstreamError(f"{path}: {e}") from e
    if not isinstance(data, dict):
        raise UpstreamError(f"{path}: not a JSON object")
    return data


@lru_cache(maxsize=None)
def lock(path: Path = LOCK_FILE) -> dict:
    data = _read(path)
    ups = data.get("upstreams")
    if not isinstance(ups, dict) or not ups:
        raise UpstreamError(f"{path}: no `upstreams` map")
    return ups


def entry(name: str) -> dict:
    ups = lock()
    if name not in ups:
        raise UpstreamError(f"no upstream {name!r} in {LOCK_FILE}")
    return ups[name]


def lock_id(name: str, ent: dict | None = None) -> str:
    """`<name>@<12 hex>` over the canonical JSON of the lock entry."""
    ent = entry(name) if ent is None else ent
    canon = json.dumps(ent, sort_keys=True, separators=(",", ":"))
    return f"{name}@{hashlib.sha256(canon.encode()).hexdigest()[:12]}"


@lru_cache(maxsize=None)
def facts(name: str) -> dict:
    """The facts file for `name`, refused when it was not verified against the
    current lock entry."""
    ent = entry(name)
    data = _read(UPSTREAMS_DIR / ent["facts"])
    want, have = lock_id(name), data.get("verified_against")
    if have != want:
        raise UpstreamError(
            f"{ent['facts']} was verified against {have!r} but the lock is now {want!r}; "
            "re-verify its facts against the new version, then update verified_against "
            "(references/upstreams/README.md)")
    return data


def fact(name: str, key: str):
    """The value of one fact (facts are {"value": ..., "source": ...})."""
    f = facts(name).get("facts", {})
    if key not in f:
        raise UpstreamError(f"{entry(name)['facts']}: no fact {key!r}")
    return f[key]["value"]


def pin(name: str) -> str:
    """`<ref>@<7-hex commit>`, the form doctor reports and compares."""
    ent = entry(name)
    return f"{ent['ref']}@{ent['commit'][:7]}"


# Tests only: BLIS builds and catalog commits a test apparatus adds on top of
# the lock (tests never edit lock.json). Empty in normal use.
_TEST_BLIS_BINARIES: dict[str, dict] = {}
_TEST_CATALOG_COMMITS: set[str] = set()


def blis_binary(sha256: str) -> tuple[str, dict] | None:
    """The locked BLIS binary with this sha256 (optional `sha256:` prefix)."""
    want = sha256.split(":")[-1].lower()
    binaries = {**entry("blis").get("binaries", {}), **_TEST_BLIS_BINARIES}
    for bid, b in binaries.items():
        if b.get("sha256") == want:
            return bid, b
    return None


def blis_catalog_locked(commit: str) -> bool:
    return commit == entry("blis").get("catalog", {}).get("commit") or \
        commit in _TEST_CATALOG_COMMITS


def representability_table_md() -> str:
    """The BLIS representability table in method-plan.md, generated from
    blis.json so the doc can't drift from what the adapter enforces."""
    rep = [k["doc"] for k in fact("blis", "representable_knobs")]
    rej = [k["doc"] for k in fact("blis", "not_representable")]
    rows = ["| Representable | Not representable |", "|---|---|"]
    for i in range(max(len(rep), len(rej))):
        a = rep[i] if i < len(rep) else ""
        b = rej[i] if i < len(rej) else ""
        rows.append(f"| {a} | {b} |".replace("|  |", "| |"))
    return "\n".join(rows) + "\n"
