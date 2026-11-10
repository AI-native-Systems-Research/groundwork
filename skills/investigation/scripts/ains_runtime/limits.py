"""Limit labels and required bounds (plan §4.5; spec §9; decisions D7, D8).

What this module does, and no more:

  - labels every limit with who enforces it: `runtime | engine | advisory`
  - checks the bounds the runtime requires before launch (every Nous
    request carries `max_iterations`; Nous enforces it)
  - refuses a label that over-promises: the runtime never enforces time or
    money (D7), and a monetary figure is never an engine-enforced cap at
    the pinned engines (plan §1 Q3: `--max-cost` is advisory)
  - reports elapsed wall-clock time against any stated time budget, as
    advisory only. Nothing here ever stops a run.

There are no invocation-count caps (D8). The runtime-enforced guards are
RUNTIME_GUARDS below: one approval per invocation, no automatic retry or
chaining, and the duplicate check on equivalent material.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Iterable, Mapping

RUNTIME, ENGINE, ADVISORY = "runtime", "engine", "advisory"
ENFORCED_BY = (RUNTIME, ENGINE, ADVISORY)

RUNTIME_GUARDS = (
    {"name": "approvals_per_invocation", "value": 1, "enforced_by": RUNTIME},
    {"name": "automatic_retry_or_chaining", "value": "never", "enforced_by": RUNTIME},
    {"name": "equivalent_invocations_without_repeat_of", "value": 1,
     "enforced_by": RUNTIME},
)

# Request limits a capability must carry (plan §4.5). The engine enforces
# them; the runtime only refuses a launch without them.
REQUIRED_BOUNDS: Mapping[str, tuple[str, ...]] = {"nous": ("max_iterations",)}

_MONEY_RE = re.compile(r"(usd|cost|budget|spend|dollar|price)", re.IGNORECASE)
_TIME_RE = re.compile(r"(?:^|_)(wall|time|duration|elapsed|deadline)|"
                      r"_(s|sec|secs|seconds|m|min|mins|minutes|h|hr|hrs|hours)$",
                      re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "sec": 1, "secs": 1, "seconds": 1, "m": 60, "min": 60,
                 "mins": 60, "minutes": 60, "h": 3600, "hr": 3600, "hrs": 3600,
                 "hours": 3600}


def is_money(name: str) -> bool:
    return bool(_MONEY_RE.search(name))


def is_time(name: str) -> bool:
    return bool(_TIME_RE.search(name)) and not is_money(name)


def time_seconds(name: str, value: Any) -> float | None:
    """A time budget in seconds, from a name's unit suffix (default seconds)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    m = re.search(r"_([a-z]+)$", name.lower())
    unit = _UNIT_SECONDS.get(m.group(1), 1) if m else 1
    return float(value) * unit


def required_bound_problems(capability: str, limits: Mapping[str, Any] | None) -> list[str]:
    out = []
    limits = limits or {}
    for name in REQUIRED_BOUNDS.get(capability, ()):
        v = limits.get(name)
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            out.append(f"limits.{name}: required integer >= 1 for {capability} "
                       "(the engine enforces it; the runtime refuses a launch without it)")
    return out


def label_problems(labelled: Iterable[Mapping[str, Any]]) -> list[str]:
    out = []
    for x in labelled:
        name, by = str(x.get("name", "")), x.get("enforced_by")
        if by not in ENFORCED_BY:
            out.append(f"limit {name!r}: enforced_by must be one of {ENFORCED_BY}")
        elif is_money(name) and by != ADVISORY:
            out.append(f"limit {name!r}: monetary limits are advisory only "
                       f"(labelled {by!r})")
        elif is_time(name) and by == RUNTIME:
            out.append(f"limit {name!r}: the runtime never enforces time (D7)")
    return out


def labelled(record: Mapping[str, Any]) -> list[dict]:
    """Every limit of a preparation, labelled.

    The adapter's own labels (record material `limits`) come first. Any
    request limit the adapter did not label is advisory: nothing enforces it.
    """
    mat = record.get("material", {})
    out = [dict(x) for x in mat.get("limits", [])]
    have = {x.get("name") for x in out}
    for k, v in sorted((mat.get("request", {}).get("limits") or {}).items()):
        if k not in have:
            out.append({"name": k, "value": v, "enforced_by": ADVISORY})
    return out


def launch_problems(record: Mapping[str, Any]) -> list[str]:
    """Pre-launch limit checks on a stored preparation record."""
    mat = record.get("material", {})
    cap = record.get("capability") or mat.get("adapter", {}).get("capability", "")
    return (required_bound_problems(cap, mat.get("request", {}).get("limits"))
            + label_problems(labelled(record)))


def _ts(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def elapsed_report(invocation: Mapping[str, Any], record: Mapping[str, Any],
                   now: dt.datetime | None = None) -> dict:
    """Elapsed wall-clock time and any time budget it is compared against.

    Advisory only (D7): `exceeded` is information for the owner; the runtime
    never stops a run because of it.
    """
    start = _ts(invocation.get("created_at"))
    end = _ts(invocation.get("ended_at")) or now or dt.datetime.now(dt.timezone.utc)
    secs = (end - start).total_seconds() if start else None
    budgets = []
    for x in labelled(record):
        if is_time(str(x.get("name", ""))):
            b = time_seconds(str(x["name"]), x.get("value"))
            budgets.append({"name": x["name"], "value": x.get("value"),
                            "enforced_by": x.get("enforced_by"), "budget_s": b,
                            "exceeded": (secs is not None and b is not None and secs > b)})
    return {"elapsed_s": None if secs is None else round(secs, 3),
            "time_budgets": budgets, "note": "time limits are advisory (D7)"}


def spend_unknown(source: str) -> dict:
    """Unknown spend is unknown, never 0 (spec §9)."""
    return {"reported_usd": None, "completeness": "unknown", "source": source}
