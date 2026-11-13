#!/usr/bin/env python3
"""Fail the build if any measured file is below a per-file coverage floor.

The total floor (`.coveragerc` `fail_under = 80`) alone cannot catch the
regression class seen three times in this repo (verify_upstreams.py #2,
doctor.py parsers #3, check_skill_spec.py #8, all found by audit at 0% while
the total stayed green): at ~90% total a 126-statement file dropping to 0%
moves the total by only ~1.8 points and the build stays green. This checker
makes "file below floor" a hard failure independent of the total.

Reads the coverage data with the same .coveragerc as `coverage report` (so
`source`, `parallel` combining and `exclude_also` all apply), converts it to
JSON and asserts every measured file is at or above the floor. Files with zero
statements (empty __init__.py) always pass.

Usage: check_coverage_floor.py [FLOOR] [--data-file PATH] [--rcfile PATH]
  FLOOR: minimum percent per file, default 40
Exit codes: 0 = all files at or above the floor, 1 = at least one file below.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

DEFAULT_FLOOR = 40


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Fail if any measured file's coverage is below a floor.")
    parser.add_argument("floor", nargs="?", type=float, default=DEFAULT_FLOOR,
                        help=f"minimum percent per file (default {DEFAULT_FLOOR})")
    parser.add_argument("--data-file", default=None,
                        help="coverage data file (default: .coveragerc's)")
    parser.add_argument("--rcfile", default=None,
                        help="coverage config (default: .coveragerc)")
    args = parser.parse_args(argv)

    try:
        import coverage
    except ImportError:
        print("coverage is not installed; cannot check the per-file floor",
              file=sys.stderr)
        return 1

    # data_file=None means "no disk file" and config_file=None means "no
    # config" to coverage 7, so only pass them when actually set.
    kwargs = {}
    if args.data_file:
        kwargs["data_file"] = args.data_file
    if args.rcfile:
        kwargs["config_file"] = args.rcfile
    cov = coverage.Coverage(**kwargs)
    report: dict = {}
    try:
        cov.load()
        # json_report writes to a file; write to a temp file we control and
        # always remove it, success or failure.
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
            out = tf.name
        try:
            cov.json_report(outfile=out)
            report = json.loads(Path(out).read_text(encoding="utf-8"))
        finally:
            Path(out).unlink(missing_ok=True)
    except coverage.exceptions.CoverageException as e:
        print(f"cannot read coverage data: {e}", file=sys.stderr)
        return 1

    below: list[tuple[str, float]] = []
    lowest: tuple[str, float] | None = None
    for fname, finfo in sorted(report["files"].items()):
        s = finfo["summary"]
        if not s["num_statements"]:
            continue
        pct = s["percent_covered"]
        if lowest is None or pct < lowest[1]:
            lowest = (fname, pct)
        if pct < args.floor:
            below.append((fname, pct))

    if below:
        print(f"per-file coverage floor {args.floor:g}%: {len(below)} file(s) below",
              file=sys.stderr)
        for fname, pct in below:
            print(f"  {pct:5.1f}%  {fname}", file=sys.stderr)
        return 1

    files = len(report["files"])
    total = report["totals"]["percent_covered"]
    if lowest is not None:
        print(f"OK per-file floor {args.floor:g}%: {files} files, lowest {lowest[0]} "
              f"at {lowest[1]:.1f}%, total {total:.1f}%")
    else:  # data held only zero-statement files
        print(f"OK per-file floor {args.floor:g}%: {files} files, none with "
              f"statements, total {total:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
