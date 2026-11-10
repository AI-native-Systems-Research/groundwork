"""Fake engine: the R0 tree. Starts one child in its own process group and
one child with start_new_session=True (as signal-pipeline does for every
`claude` child), records their pids in state.json, then sleeps.

A signal to the engine's group alone does NOT reach the new-session child.
"""
import subprocess
import sys

from _common import parse, sleep_bounded, started

w = parse()
child = [sys.executable, "-c",
         "import time,sys; end=time.monotonic()+60\n"
         "while time.monotonic()<end: time.sleep(0.05)", str(w)]
same = subprocess.Popen(child)
new = subprocess.Popen(child, start_new_session=True)
started(w, same_group_child=same.pid, new_session_child=new.pid)
sleep_bounded()
