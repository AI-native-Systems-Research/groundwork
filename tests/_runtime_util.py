"""Shared helpers for tests/test_runtime_*.py (not a test module)."""
import copy
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills/investigation/scripts"
RUNTIME = SCRIPTS / "runtime.py"
SCHEMAS = REPO / "skills/investigation/references/runtime-schemas"
EXAMPLES = SCHEMAS / "examples"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

COMMIT = "0123456789abcdef0123456789abcdef01234567"
DIGEST = "sha256:" + "ab" * 32
TARGET = {"repo_name": "demo-repo", "commit": COMMIT}


def envelope(mtype, message_id="m-1", body=None, *, work_id="w-1", revision=1,
             reply_to=None, artifacts=None):
    m = {"schema": f"ains.{mtype}/v1", "message_id": message_id, "work_id": work_id,
         "revision": revision, "target": dict(TARGET), "artifacts": artifacts or [],
         "body": body if body is not None else {}}
    if reply_to is not None:
        m["reply_to"] = reply_to
    return m


def request(message_id="req-1", *, work_id="w-1", revision=1, **body_over):
    body = {"capability": "nous", "question": "Does weight 2 beat weight 1?",
            "task_type": "verify", "objective": "minimize p99 TTFT",
            "baseline": "weight 1", "hypothesis": "weight 2 lowers p99 TTFT",
            "locks": {}, "limits": {"max_iterations": 3},
            "semantics": {"models": {"session": "model-x"}, "run_id": "r1"}}
    body.update(body_over)
    return envelope("experiment-request", message_id, body, work_id=work_id,
                    revision=revision)


def mutate(msg, fn):
    m = copy.deepcopy(msg)
    fn(m)
    return m


def kill_matching(token: str) -> list[int]:
    """SIGKILL every live process whose command line contains token (test
    cleanup: token is a unique temp-dir name, so only this test's
    supervisors, fake engines and their children match)."""
    import os
    import signal
    import subprocess
    pids = []
    proc = Path("/proc")
    if sys.platform.startswith("linux") and proc.is_dir():
        for d in proc.iterdir():
            if d.name.isdigit():
                try:
                    cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                        "utf-8", "replace")
                except OSError:
                    continue
                if token in cmd:
                    pids.append(int(d.name))
    else:
        out = subprocess.run(["ps", "-ax", "-ww", "-o", "pid=", "-o", "command="],
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            t = line.strip().split(None, 1)
            if len(t) == 2 and token in t[1]:
                pids.append(int(t[0]))
    pids = [p for p in pids if p != os.getpid()]
    for p in pids:
        try:
            os.kill(p, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return pids


def wait_for(fn, timeout=15.0, interval=0.02, what="condition"):
    """Poll fn() until it returns a truthy value; fail after timeout seconds."""
    import time
    end = time.monotonic() + timeout
    while True:
        v = fn()
        if v:
            return v
        if time.monotonic() > end:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        time.sleep(interval)
