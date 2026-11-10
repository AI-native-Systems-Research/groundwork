#!/usr/bin/env python3
"""AINS local runtime CLI. Thin entry point; the logic is in ains_runtime/.

    runtime.py [--study DIR | --db PATH] submit <msg.json|->
    runtime.py [--study DIR | --db PATH] show <id> [--json]
    runtime.py --study DIR export
    runtime.py --study DIR prepare <work-id> [--target REPO] [--json]
    runtime.py --study DIR approve <prep-id> --code <short> --source chat --quote "<reply>"
    runtime.py --study DIR launch <prep-id>
    runtime.py --study DIR status <inv-id|work-id>
    runtime.py --study DIR cancel <inv-id> [--interrupt] [--reason TEXT]
    runtime.py --study DIR recover
    runtime.py --study DIR reconcile <inv-id> --mark-not-launched --quote "<reply>"

See ains_runtime/cli.py for the commands and exit codes, and
references/runtime-schemas/ for the message contracts.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True  # an install inside a target repo must not dirty it (D4)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ains_runtime.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
