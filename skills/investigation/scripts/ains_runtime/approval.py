"""Chat-sourced approval (plan §1 Q1, decision D1; spec §7 amendment).

The owner-facing path is the owner's agent chat:

  1. `prepare` renders an approval card (render_card): material summary,
     limits, inferred defaults, invocation counts (D8), the full material
     digest and its 12-hex short code. Plain Markdown, at most
     MAX_CARD_BYTES, no raw file contents. It is printed AND written to
     prepared/<prep-id>/card.md, byte-identical.
  2. The agent pastes the card verbatim into its own reply (tool output is
     not reliably shown to the owner, R0 §1.7 item 2).
  3. The owner gives the go in chat, quoting the short code.
  4. The agent runs `approve <prep-id> --code <short> --source chat
     --quote "<owner's reply>"`; approve() checks it and records a
     single-use approval bound to that one digest.

approve() refuses: an unknown preparation; a source other than "chat"; a
missing or wrong code; a missing quote, or a quote that does not contain the
short code; a preparation superseded by a newer request revision; a
preparation whose stored record no longer matches its digest; and a second
approval of the same preparation. Consumption happens in
Store.reserve_invocation (R3 launch), atomically with the reservation.

This records consent given in chat. It does not prove a human gave it.
"""
from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Any, Mapping

from .canonical import canonical_json, material_digest, short_code
from .store import Store, StoreError

SOURCES = ("chat",)
MAX_CARD_BYTES = 6000
MAX_VALUE_CHARS = 240
MAX_ITEMS = 12
CODE_RE = re.compile(r"^[0-9a-f]{12}$")


class ApprovalError(Exception):
    """Approval refused. Message names the reason; never echoes secrets."""


@dataclass
class ApprovalResult:
    approval_id: str
    prep_id: str
    material_digest: str
    short_code: str


# --- card -------------------------------------------------------------------

def _clip(v: Any, n: int = MAX_VALUE_CHARS) -> str:
    s = v if isinstance(v, str) else canonical_json(v)
    s = " ".join(s.split())  # one line: no Markdown structure injected by values
    return s if len(s) <= n else s[: n - 1] + "…"


def _items(pairs: list[tuple[str, Any]]) -> list[str]:
    out = [f"- {k}: {_clip(v)}" for k, v in pairs[:MAX_ITEMS]]
    if len(pairs) > MAX_ITEMS:
        out.append(f"- … {len(pairs) - MAX_ITEMS} more (see `show <prep-id> --json`)")
    return out or ["- (none)"]


def render_card(record: dict, prepared, counts: Mapping[str, int], *,
                approve_hint: str = "runtime.py") -> str:
    """The approval card for one preparation (bounded, deterministic)."""
    m = record["material"]
    req = m["request"]
    tgt = m["target"]
    mdig = record["material_digest"]
    code = short_code(mdig)
    pid = record["prep_id"]
    head = [
        f"## Approval card: {pid}",
        "",
        f"- capability: {record['capability']} (adapter {m['adapter']['version']}, "
        f"runtime {m['runtime_version']})",
        f"- material digest: `{mdig}`",
        f"- short code: **{code}**",
        "",
    ]
    body = ["### What runs", ""]
    body += _items([
        ("target", f"{tgt['repo_name']} @ {tgt['commit']} "
                   f"({'clean' if tgt['clean'] else 'DIRTY'})"),
        ("question", req["question"]), ("task type", req["task_type"]),
        ("objective", req["objective"]), ("baseline", req.get("baseline") or "-"),
        ("hypothesis", req.get("hypothesis") or "-"),
        ("argv", " ".join(record["argv"])), ("cwd", record["cwd"]),
    ])
    body += ["", "### Semantics and locks", ""]
    body += _items(sorted(req["semantics"].items()) +
                   [(f"lock {k}", v) for k, v in sorted(req["locks"].items())])
    body += ["", "### Limits", ""]
    lim = [(f"{x['name']} ({x.get('enforced_by', 'advisory')})", x.get("value"))
           for x in m.get("limits", [])]
    lim += [(f"request {k}", v) for k, v in sorted(req["limits"].items())
            if k not in {x["name"] for x in m.get("limits", [])}]
    body += _items(lim)
    body += ["", "### Inferred defaults", ""]
    body += _items([(d["field"], f"{_clip(d.get('value'), 80)} ({d.get('reason', '')})")
                    for d in m.get("defaults", [])])
    body += ["", "### Inputs and environment", ""]
    env_items = [(f"input {r['location']}", r["digest"]) for r in record["inputs"]]
    env_items += [(f"env ${k}", v if v is not None else "unset")
                  for k, v in sorted(m["env"]["vars"].items())]
    env_items += [(f"file {k}", v) for k, v in sorted(m["env"]["files"].items())]
    env_items += [(f"secret ${k}", v) for k, v in sorted(record["env_secrets"].items())]
    env_items.append(("recorded, not material (D9)",
                      f"{len(record['env_observed'])} ANTHROPIC_*/CLAUDE_* variables"))
    body += _items(env_items)
    extra = list(getattr(prepared, "summary", []) or [])
    if extra:
        body += ["", "### Adapter notes", ""] + [f"- {_clip(x)}" for x in extra[:MAX_ITEMS]]
    ns = record.get("next_step")
    if isinstance(ns, dict) and ns:
        # Spec §14: a next step needs fresh justification; the owner sees it
        # here, beside what they approve. Not material (plan §4.2).
        body += ["", "### Next step justification (from plan.md's step log)", ""]
        body += _items([(k, ns.get(k)) for k in ("interpretation_of_prior",
                        "remaining_uncertainty", "expected_information_gain",
                        "rationale")])
    body += ["", "### Counts (D8: no caps)", "",
             f"- invocations recorded in this study: {counts.get('study', 0)}",
             f"- of capability {record['capability']}: {counts.get('capability', 0)}"]
    tail = [
        "",
        "### To approve",
        "",
        f"Reply in chat with your go and the short code **{code}**. The agent then runs:",
        "",
        f"    {approve_hint} approve {pid} --code {code} --source chat "
        "--quote \"<your reply>\"",
        "",
        "An approval is single-use and bound to this digest. Any material change "
        "needs a new preparation and a new approval.",
        "",
    ]
    card = "\n".join(head + body + tail)
    if len(card.encode("utf-8")) > MAX_CARD_BYTES:
        note = ["", "- … card truncated to its size bound; full record: "
                f"`runtime.py show {pid} --json`"]
        budget = MAX_CARD_BYTES - len("\n".join(head + note + tail).encode("utf-8")) - 1
        kept, used = [], 0
        for line in body:
            n = len(line.encode("utf-8")) + 1
            if used + n > budget:
                break
            kept.append(line)
            used += n
        card = "\n".join(head + kept + note + tail)
    return card


# --- approve ----------------------------------------------------------------

def approve(store: Store, prep_id: str, *, code: str | None, source: str | None,
            quote: str | None, env: Mapping[str, str] | None = None) -> ApprovalResult:
    """Check and record one chat-sourced approval (module docstring)."""
    p = store.get_preparation(prep_id)
    if p is None:
        raise ApprovalError(f"no preparation {prep_id!r} in this runtime DB")
    mdig = p["material_digest"]
    expected = short_code(mdig)
    if source not in SOURCES:
        raise ApprovalError(f"--source must be one of {', '.join(SOURCES)} (D1)")
    if not code or not code.strip():
        raise ApprovalError("--code is required: the short code from the approval card")
    code = code.strip()
    if not CODE_RE.match(code) or code != expected:
        raise ApprovalError("--code does not match this preparation's short code; "
                            "show the owner the current card and ask again")
    if not quote or not quote.strip():
        raise ApprovalError("--quote is required: the owner's chat reply, verbatim")
    if expected not in quote:
        raise ApprovalError("--quote must contain the short code: the owner quotes it "
                            "in the go (D1)")
    rec = p["record"]
    if material_digest(rec.get("material", {})) != mdig:
        raise ApprovalError("stored preparation does not match its digest; prepare again")
    work = store.get_work(p["work_id"])
    if work is None or work["revision"] != p["revision"]:
        raise ApprovalError("the work has a newer request revision; prepare it again")
    if store.get_approval(prep_id) is not None:
        raise ApprovalError(f"preparation {prep_id!r} is already approved (an approval "
                            "is single-use; a new run needs a new preparation)")
    aid = "appr-" + secrets.token_hex(6)
    try:
        store.insert_approval(aid, prep_id, mdig, source, quote, env=env)
    except StoreError as e:
        raise ApprovalError(str(e)) from e
    return ApprovalResult(aid, prep_id, mdig, expected)
