#!/usr/bin/env python3
"""Test-only: runtime.py's CLI with the fake adapter registered.

Subprocess tests run this instead of runtime.py so the shipped CLI never has
an environment or plugin hook for loading adapters.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _runtime_fake_adapter as fake  # noqa: E402
from ains_runtime.cli import main  # noqa: E402

if __name__ == "__main__":
    fake.use()
    sys.exit(main())
