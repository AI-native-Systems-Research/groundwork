"""Fake engine: prints the campaign secret to stdout and stderr, then completes.

The engine legitimately holds the secret values (the supervisor's env_pass
carries the request's secret_vars), and real engines echo them in verbose
SDK logging, curl -v or tracebacks. This fixture pins that the supervisor
redacts the engine log at exit, so the leaked bytes never reach the file
results.py digests as immutable evidence.
"""
import os
import sys

from _common import final, parse, started

w = parse()
started(w)
print(f"connecting with token {os.environ.get('FAKE_API_TOKEN')}")
print(f"token prefix check: {os.environ.get('FAKE_API_TOKEN', '')[:6]}",
      file=sys.stderr)
final(w, "completed")
