"""Fake engine: writes partial artifacts, then crashes (no final record)."""
import os

from _common import parse, started

w = parse()
started(w)
(w / "partial.txt").write_text("iteration 1 of 3\n")
os._exit(3)
