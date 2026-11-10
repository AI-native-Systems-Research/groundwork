"""Fake engine: writes native output and completes (exit 0)."""
from _common import final, parse, started

w = parse()
started(w)
final(w, "completed")
