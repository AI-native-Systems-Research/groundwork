"""runtime.py command dispatch (plan §3.4).

R1 implements `submit`, `show` and `export`; R2 adds `prepare` and `approve`;
R3 adds `launch`, `status`, `cancel`, `recover`, `reconcile` and the private
`_supervise` (started only by `launch`); R7 adds `collect` and `result`. Later
phases add commands by
appending to COMMANDS a (name, help, add_arguments, handler) entry:
prepare/approve (R2), launch/status/cancel/recover/reconcile and the private
_supervise (R3), collect/result (R7). A handler gets (args, ctx) and returns
an exit code; ctx.store() opens the study's DB lazily.

Locating the DB:
  --study DIR   the study directory (investigations/<slug>); the DB is at
                $RUNS/<repo>/<slug>/.runtime/runtime.sqlite3, with <repo> from
                the study's git toplevel (_config.repo_name) and <slug> the
                directory name
  --db PATH     explicit DB path (tests, or a study outside a git repo)

Exit codes: 0 ok (submit: accepted or duplicate), 1 refused (rejected,
conflict, not found), 2 usage / store error.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import RUNTIME_VERSION
from .canonical import CanonicalError, pretty_json
from .store import DB_NAME, Store, StoreError, load_message_file, open_store

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import _config  # noqa: E402


class UsageError(Exception):
    pass


@dataclass
class Context:
    study: Path | None
    db: Path | None
    _store: Store | None = None

    def db_path(self) -> Path:
        if self.db is not None:
            return self.db
        if self.study is None:
            raise UsageError("give --study DIR (or --db PATH)")
        top = _config.repo_toplevel(self.study)
        if top is None:
            raise UsageError(f"{self.study}: not inside a git repo; use --db")
        return (_config.runs_dir(_config.repo_name(top), self.study.resolve().name)
                / ".runtime" / DB_NAME)

    def repo_name(self) -> str | None:
        if self.study is None:
            return None
        top = _config.repo_toplevel(self.study)
        return _config.repo_name(top) if top else None

    def store(self, create: bool = True) -> Store:
        if self._store is None:
            self._store = open_store(self.db_path(), create=create)
        return self._store

    def close(self) -> None:
        if self._store is not None:
            self._store.close()


def _print(obj) -> None:
    sys.stdout.write(pretty_json(obj))


# --- submit -----------------------------------------------------------------

def _submit_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("message", help="message JSON file, or - for stdin")


def cmd_submit(args, ctx: Context) -> int:
    try:
        msg = load_message_file(args.message)
    except (OSError, CanonicalError) as e:
        print(f"submit: {e}", file=sys.stderr)
        return 2
    expected = ctx.repo_name()
    if (expected and isinstance(msg, dict) and isinstance(msg.get("target"), dict)
            and msg["target"].get("repo_name") not in (None, expected)):
        _print({"status": "rejected", "reasons": [
            f"target.repo_name: {msg['target']['repo_name']!r} is not this study's "
            f"repo {expected!r}"]})
        return 1
    res = ctx.store().submit(msg)
    _print(res.output())
    if not res.ok:
        for r in res.reasons:
            print(f"submit: {r}", file=sys.stderr)
    return 0 if res.ok else 1


# --- show -------------------------------------------------------------------

def _show_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("id", help="work id, preparation id, invocation id or message id")
    p.add_argument("--json", action="store_true", help="machine-readable output")


def lookup(store: Store, ident: str) -> tuple[str, dict] | None:
    """Resolve an id to (kind, view). Read-only."""
    w = store.get_work(ident)
    if w is not None:
        return "work", {**w,
                        "messages": [m["message_id"] for m in store.list_messages(ident)],
                        "preparations": [p["prep_id"] for p in
                                         store.list_preparations(ident)],
                        "invocations": [i["inv_id"] for i in
                                        store.list_invocations(ident)],
                        "events": store.list_events(work_id=ident)}
    p = store.get_preparation(ident)
    if p is not None:
        return "preparation", {**p, "approval": store.get_approval(ident)}
    i = store.get_invocation(ident)
    if i is not None:
        return "invocation", {**i, "events": store.list_events(entity_id=ident)}
    m = store.get_message(ident)
    if m is not None:
        return "message", m
    return None


def cmd_show(args, ctx: Context) -> int:
    try:
        store = ctx.store(create=False)
    except StoreError as e:
        print(f"show: {e}", file=sys.stderr)
        return 1
    found = lookup(store, args.id)
    if found is None:
        print(f"show: no work, preparation, invocation or message {args.id!r}",
              file=sys.stderr)
        return 1
    kind, view = found
    if args.json:
        _print({"kind": kind, **view})
        return 0
    print(f"{kind} {args.id}")
    for k, v in view.items():
        if isinstance(v, (dict, list)):
            if k == "events":
                for e in v:
                    print(f"  event  {e['at']}  {e['entity']}:{e['entity_id']}  {e['event']}")
            else:
                print(f"  {k}: {pretty_json(v).strip() if isinstance(v, dict) else ', '.join(map(str, v)) or '-'}")
        else:
            print(f"  {k}: {v if v is not None else '-'}")
    return 0


# --- export -----------------------------------------------------------------

def _export_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", type=Path,
                   help="study dir to write runtime/ into (default: --study)")


def cmd_export(args, ctx: Context) -> int:
    from .export import export_study
    study = args.out or ctx.study
    if study is None:
        raise UsageError("export needs --study DIR (or --out DIR)")
    try:
        index = export_study(ctx.store(create=False), study)
    except StoreError as e:
        print(f"export: {e}", file=sys.stderr)
        return 1
    _print({"exported_to": str(Path(study) / "runtime"), "counts": index["counts"]})
    return 0


# --- prepare / approve (R2) -------------------------------------------------

def _prepare_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("work_id", help="work id of a submitted experiment-request")
    p.add_argument("--target", type=Path,
                   help="target repo (default: the --study dir's git toplevel)")
    p.add_argument("--json", action="store_true",
                   help="print {prep_id, material_digest, short_code, card_path, card} "
                        "instead of the card")


def _hint(ctx: Context) -> str:
    if ctx.study is not None:
        return f"runtime.py --study {ctx.study}"
    return f"runtime.py --db {ctx.db}"


def cmd_prepare(args, ctx: Context) -> int:
    from .preparation import PrepareError, prepare
    target = args.target or ctx.study
    if target is None:
        raise UsageError("prepare needs --target REPO (or --study DIR)")
    try:
        res = prepare(ctx.store(create=False), args.work_id, target=target,
                      approve_hint=_hint(ctx))
    except PrepareError as e:
        for r in e.reasons:
            print(f"prepare: {r}", file=sys.stderr)
        return 1
    if args.json:
        _print({"prep_id": res.prep_id, "material_digest": res.material_digest,
                "short_code": res.short_code, "card_path": str(res.card_path),
                "card": res.card})
    else:
        # Exactly the bytes of card.md, so the agent can paste it verbatim.
        sys.stdout.write(res.card)
    return 0


def _approve_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("prep_id")
    p.add_argument("--code", help="the card's 12-hex short code, as quoted by the owner")
    p.add_argument("--source", help="where the owner's go was given: chat (D1)")
    p.add_argument("--quote", help="the owner's chat reply, verbatim")


def cmd_approve(args, ctx: Context) -> int:
    from .approval import ApprovalError, approve
    try:
        res = approve(ctx.store(create=False), args.prep_id, code=args.code,
                      source=args.source, quote=args.quote)
    except ApprovalError as e:
        print(f"approve: {e}", file=sys.stderr)
        return 1
    _print({"approval_id": res.approval_id, "prep_id": res.prep_id,
            "material_digest": res.material_digest, "short_code": res.short_code,
            "single_use": True})
    return 0


# --- launch / status / cancel / recover / reconcile (R3) --------------------

def _launch_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("prep_id")


def cmd_launch(args, ctx: Context) -> int:
    from .supervisor import LaunchError, launch
    try:
        res = launch(ctx.store(create=False), args.prep_id)
    except LaunchError as e:
        for r in e.reasons:
            print(f"launch: {r}", file=sys.stderr)
        return 1
    for w in res.warnings:
        print(f"launch: warning: {w}", file=sys.stderr)
    _print(res.as_dict())
    return 0


def _status_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("id", help="invocation id or work id")


def cmd_status(args, ctx: Context) -> int:
    from .recovery import RecoveryError, status
    try:
        _print(status(ctx.store(create=False), args.id))
    except RecoveryError as e:
        print(f"status: {e}", file=sys.stderr)
        return 1
    return 0


def _cancel_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("inv_id")
    p.add_argument("--interrupt", action="store_true",
                   help="adapter-specific stronger stop (Nous: SIGINT, resumable)")
    p.add_argument("--reason", help="why the owner asked to cancel")


def cmd_cancel(args, ctx: Context) -> int:
    from .recovery import RecoveryError, cancel
    try:
        _print(cancel(ctx.store(create=False), args.inv_id, interrupt=args.interrupt,
                      reason=args.reason))
    except RecoveryError as e:
        print(f"cancel: {e}", file=sys.stderr)
        return 1
    return 0


def _recover_args(p: argparse.ArgumentParser) -> None:
    pass


def cmd_recover(args, ctx: Context) -> int:
    from .recovery import recover
    _print(recover(ctx.store(create=False)))
    return 0


def _reconcile_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("inv_id")
    p.add_argument("--mark-not-launched", action="store_true", required=True,
                   help="the owner confirmed in chat that this recovery_required "
                        "invocation never launched (its approval stays used)")
    p.add_argument("--quote", help="the owner's chat reply, verbatim")


def cmd_reconcile(args, ctx: Context) -> int:
    from .recovery import RecoveryError, mark_not_launched
    try:
        _print(mark_not_launched(ctx.store(create=False), args.inv_id, quote=args.quote))
    except RecoveryError as e:
        print(f"reconcile: {e}", file=sys.stderr)
        return 1
    return 0


# --- collect / result (R7) ---------------------------------------------------

def _collect_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("inv_id")


def cmd_collect(args, ctx: Context) -> int:
    from .results import ResultError, collect
    try:
        out = collect(ctx.store(create=False), args.inv_id, study=ctx.study)
    except ResultError as e:
        print(f"collect: {e}", file=sys.stderr)
        return 1
    _print(out)
    return 0


def _result_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("inv_id")
    p.add_argument("--answer", type=Path,
                   help="answer.json to validate (default: <study>/final/answer.json, "
                        "else the engine's own answer.json)")
    p.add_argument("--outcome", choices=("supported", "not_supported", "inconclusive",
                                         "other"),
                   help="the coordinator's classification against the request's "
                        "hypothesis (completed, collected runs only)")
    p.add_argument("--explanation", help="required with --outcome")
    p.add_argument("--out", type=Path,
                   help="study dir to export runtime/ into (default: --study)")


def cmd_result(args, ctx: Context) -> int:
    from .export import export_study
    from .results import ResultError, build
    if args.explanation is not None and args.outcome is None:
        raise UsageError("--explanation needs --outcome")
    store = ctx.store(create=False)
    try:
        res = build(store, args.inv_id, study=ctx.study, answer=args.answer,
                    outcome=(args.outcome, args.explanation) if args.outcome else None)
    except ResultError as e:
        print(f"result: {e}", file=sys.stderr)
        return 1
    study = args.out or ctx.study
    out = {"result": res["message"], "notes": res["notes"]}
    if study is not None:
        export_study(store, study)
        out["exported_to"] = str(Path(study) / "runtime" / "results" /
                                 f"{args.inv_id}.json")
    else:
        out["exported_to"] = None
        out["notes"] = out["notes"] + ["not exported: give --study (or --out) to write "
                                       "$STUDY/runtime/results/"]
    _print(out)
    return 0


def _supervise_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("inv_id")


def cmd_supervise(args, ctx: Context) -> int:
    from .supervisor import main_supervise
    return main_supervise(ctx.db_path(), args.inv_id)


Handler = Callable[[argparse.Namespace, Context], int]
COMMANDS: list[tuple[str, str, Callable[[argparse.ArgumentParser], None], Handler]] = [
    ("submit", "validate and record a message; prints the receipt (idempotent)",
     _submit_args, cmd_submit),
    ("show", "read-only view of a work item, preparation, invocation or message",
     _show_args, cmd_show),
    ("export", "rewrite $STUDY/runtime/ review copies from the DB",
     _export_args, cmd_export),
    ("prepare", "validate and prepare a work item; prints the approval card",
     _prepare_args, cmd_prepare),
    ("approve", "record the owner's chat go for one preparation (single-use)",
     _approve_args, cmd_approve),
    ("launch", "revalidate, reserve atomically, start the supervisor; returns at once",
     _launch_args, cmd_launch),
    ("status", "reconcile with native state and print the lifecycle state",
     _status_args, cmd_status),
    ("cancel", "record a cancel request and call the adapter's stop mechanism",
     _cancel_args, cmd_cancel),
    ("recover", "reconcile invocations stuck in flight; never relaunches",
     _recover_args, cmd_recover),
    ("reconcile", "resolve a recovery_required invocation as never launched",
     _reconcile_args, cmd_reconcile),
    ("collect", "read-only collection of a terminal invocation; retryable, never reruns",
     _collect_args, cmd_collect),
    ("result", "build the experiment-result record (validator run) and export it",
     _result_args, cmd_result),
]
# Private: started only by `launch`, hidden from --help.
PRIVATE_COMMANDS = [("_supervise", _supervise_args, cmd_supervise)]


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="runtime.py",
                                 description="AINS local runtime (governed launches).")
    ap.add_argument("--version", action="version", version=f"ains-runtime {RUNTIME_VERSION}")
    ap.add_argument("--study", type=Path, help="study dir, investigations/<slug>")
    ap.add_argument("--db", type=Path, help="explicit runtime DB path")
    sub = ap.add_subparsers(dest="command", required=True)
    for name, help_, add_args, handler in COMMANDS:
        p = sub.add_parser(name, help=help_)
        add_args(p)
        p.set_defaults(handler=handler)
    for name, add_args, handler in PRIVATE_COMMANDS:
        p = sub.add_parser(name)
        add_args(p)
        p.set_defaults(handler=handler)
    # Keep private commands out of the usage line and command list.
    sub.metavar = "{" + ",".join(n for n, *_ in COMMANDS) + "}"
    sub._choices_actions = [a for a in sub._choices_actions
                            if not a.dest.startswith("_")]
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    ctx = Context(study=args.study, db=args.db)
    try:
        return args.handler(args, ctx)
    except UsageError as e:
        print(f"runtime.py: {e}", file=sys.stderr)
        return 2
    except StoreError as e:
        print(f"runtime.py: {e}", file=sys.stderr)
        return 2
    finally:
        ctx.close()
