"""Fake engine: ignores SIGTERM; only SIGKILL stops it."""
import signal

from _common import parse, sleep_bounded, started

w = parse()
signal.signal(signal.SIGTERM, signal.SIG_IGN)
started(w)
sleep_bounded()
