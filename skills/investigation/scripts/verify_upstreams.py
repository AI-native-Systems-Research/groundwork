#!/usr/bin/env python3
"""Check references/upstreams/ against the upstreams actually installed here.

Read-only unless --write-docs. Run it after bumping lock.json, before you set
a facts file's verified_against to the new lock id.

    python3 verify_upstreams.py                 # report
    python3 verify_upstreams.py --json
    python3 verify_upstreams.py --write-docs    # regenerate method-plan.md's table

Checks:
  nous, spotlights   the installed engine's git sha equals the locked commit
                     (doctor's reader: direct_url.json)
  blis binaries      every locked binary under blis_apparatus_root exists and
                     its sha256 matches; every `flags` entry in a representable
                     knob is in that binary's `blis run --help`; knobs with a
                     `flag_prefix` match at least one flag
  blis catalog       the catalog checkout's HEAD equals the locked commit
  facts              entries marked `verify_note` are listed for the owner

Exit 0 when nothing failed, 1 otherwise. Warnings (a binary that isn't on this
machine, a knob marked STALE) do not fail.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _config  # noqa: E402
import doctor  # noqa: E402
import upstreams as U  # noqa: E402

METHOD_PLAN = U.UPSTREAMS_DIR.parent / "method-plan.md"
TABLE_MARK = "<!-- generated from references/upstreams/blis.json"


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def help_flags(binary: Path) -> set[str] | None:
    try:
        r = subprocess.run([str(binary), "run", "--help"], capture_output=True, text=True,
                           timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return set(re.findall(r"(?m)^\s+(?:-\w, )?(--[a-z0-9][a-z0-9-]*)", r.stdout + r.stderr))


def check_engines(report: dict, machine: dict | None = None) -> None:
    if machine is None:
        machine = _config.load_machine_config()
    pins = doctor.read_pins()
    for name, engine in (("nous", "nous"), ("spotlights", "spotlights-engine")):
        e = doctor.engine_info(engine, machine, pins, True, {})
        want = U.entry(name)["commit"]
        if not e.get("found"):
            report["warnings"].append(f"{name}: {engine} not installed here; not checked")
        elif e.get("sha") != want:
            report["failures"].append(f"{name}: installed {str(e.get('sha'))[:12]} is not the "
                                      f"locked {want[:12]}")
        else:
            report["ok"].append(f"{name}: installed sha equals the lock ({want[:12]})")


def check_blis(report: dict, machine: dict | None = None) -> None:
    if machine is None:
        machine = _config.load_machine_config()
    root = machine.get("blis_apparatus_root")
    blis = U.entry("blis")
    if not root:
        report["warnings"].append("blis: no blis_apparatus_root in the machine config; "
                                  "binaries and catalog not checked")
        return
    root = Path(str(root)).expanduser()
    knobs = U.fact("blis", "representable_knobs")
    for bid, b in blis["binaries"].items():
        p = root / b["file"]
        if not p.is_file():
            report["warnings"].append(f"blis/{bid}: {p} not on this machine")
            continue
        have = sha256(p)
        if have != b["sha256"]:
            report["failures"].append(f"blis/{bid}: {p} sha256 {have[:12]} is not the locked "
                                      f"{b['sha256'][:12]}")
            continue
        flags = help_flags(p)
        if flags is None:
            report["failures"].append(f"blis/{bid}: `run --help` failed")
            continue
        for k in knobs:
            for f in k.get("flags", ()):
                if f not in flags:
                    report["stale"].append(f"blis/{bid}: knob {k['label']!r}: {f} is not a "
                                           "flag of this binary")
            pre = k.get("flag_prefix")
            if pre and not any(f.startswith(pre) for f in flags):
                report["stale"].append(f"blis/{bid}: knob {k['label']!r}: no {pre}* flag")
        report["ok"].append(f"blis/{bid}: sha256 matches; flags checked")
    cat = root / blis["catalog"]["default_dir"]
    head = doctor.run(["git", "rev-parse", "HEAD"], cwd=cat) if cat.is_dir() else None
    if head is None:
        report["warnings"].append(f"blis catalog: {cat} is not a git checkout here")
    elif head != blis["catalog"]["commit"]:
        report["failures"].append(f"blis catalog: {cat} is at {head[:12]}, the lock says "
                                  f"{blis['catalog']['commit'][:12]}")
    else:
        report["ok"].append("blis catalog: HEAD equals the lock")


def check_notes(report: dict) -> None:
    for k in U.fact("blis", "representable_knobs"):
        if k.get("verify_note"):
            report["notes"].append(f"blis knob {k['label']!r}: {k['verify_note']}")


class WriteDocsError(Exception):
    """method-plan.md lost the generated-table anchor write_docs needs."""


def write_docs() -> bool:
    text = METHOD_PLAN.read_text(encoding="utf-8")
    try:
        s = text.index(TABLE_MARK)
        s = text.index("\n", s) + 1
        e = text.index("\n\n", s)
    except ValueError as e:
        raise WriteDocsError(
            f"{METHOD_PLAN.name}: cannot find the generated-table anchor ({e}); "
            f"restore the '{TABLE_MARK}' mark with its table followed by a blank "
            "line, or add the table by hand") from e
    new = text[:s] + U.representability_table_md().rstrip("\n") + text[e:]
    if new != text:
        METHOD_PLAN.write_text(new, encoding="utf-8")
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write-docs", action="store_true",
                    help="regenerate method-plan.md's representability table")
    a = ap.parse_args(argv)
    report: dict = {"lock_ids": {n: U.lock_id(n) for n in U.lock()}, "ok": [],
                    "warnings": [], "stale": [], "failures": [], "notes": []}
    for name in U.lock():
        try:
            U.facts(name)
        except U.UpstreamError as e:
            report["failures"].append(str(e))
    machine: dict | None = None
    if not report["failures"]:
        # Load the machine config once for both machine checks, the way
        # doctor.config_info does: a malformed file is a reported condition,
        # not a traceback (doctor prints it as UNPARSEABLE and still exits 0).
        try:
            machine = _config.load_machine_config()
        except _config.ConfigError as e:
            report["failures"].append(f"machine config: {e}")
        else:
            check_engines(report, machine)
            check_blis(report, machine)
            check_notes(report)
    if a.write_docs:
        try:
            report["docs_written"] = write_docs()
        except WriteDocsError as e:
            report["failures"].append(str(e))
    if a.json:
        print(json.dumps(report, indent=2))
    else:
        for k in ("failures", "stale", "warnings", "notes", "ok"):
            for line in report[k]:
                print(f"{k.upper():9} {line}")
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
