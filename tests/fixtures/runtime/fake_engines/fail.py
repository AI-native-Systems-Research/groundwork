"""Fake engine: writes a failed final record and exits 1."""
import sys

from _common import final, parse, started

w = parse()
started(w)
final(w, "failed")
sys.exit(1)
