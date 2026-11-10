"""Fake engine: runs until SIGTERM/SIGINT, then records `stopped` and exits.

SIGTERM -> final "stopped", exit 143; SIGINT -> final "stopped", exit 130.
A file named `go` in the work dir makes it complete normally instead.
"""
import signal
import sys

from _common import final, parse, sleep_bounded, started

w = parse()


def on(sig, _f):
    final(w, "stopped")
    sys.exit(128 + sig)


signal.signal(signal.SIGTERM, on)
signal.signal(signal.SIGINT, on)
started(w)
sleep_bounded(lambda: (w / "go").exists())
final(w, "completed")
