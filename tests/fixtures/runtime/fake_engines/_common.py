"""Shared bits of the test-only fake engines (never shipped, never real).

Every fake engine is invoked as
    python <engine>.py run <campaign.yaml> --run-id <run_id>
with FAKE_PARENT=<runs dir> in its environment (the fake adapter's env_set).
It writes its native output under $FAKE_PARENT/<run_id>/:
    state.json     at start, once signal handlers are installed ("ready")
    partial.txt    partial artifacts (crash_partial)
    final.json     {"status": "completed"|"failed"|"stopped"} on a clean end
and appends one line to $FAKE_PARENT/starts.log per start, so tests can
count how many engines actually started. Every engine bounds its own
lifetime (MAX_LIFETIME_S) so a leaked one can't run forever.
"""
import json
import os
import sys
import time
from pathlib import Path

MAX_LIFETIME_S = 60


def parse():
    a = sys.argv[1:]
    run_id = a[a.index("--run-id") + 1]
    work = Path(os.environ["FAKE_PARENT"]) / run_id
    work.mkdir(parents=True, exist_ok=True)
    return work


def started(work, **extra):
    with open(work.parent / "starts.log", "a") as f:
        f.write(f"{work.name} {os.getpid()}\n")
    write(work / "state.json", {"pid": os.getpid(), "pgid": os.getpgrp(),
                                "env_names": sorted(os.environ), **extra})


def write(path, obj):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, path)


def final(work, status):
    write(work / "final.json", {"status": status})


def sleep_bounded(stop=lambda: False):
    end = time.monotonic() + MAX_LIFETIME_S
    while time.monotonic() < end and not stop():
        time.sleep(0.02)
