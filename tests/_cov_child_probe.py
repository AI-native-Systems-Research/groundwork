"""Test-only measured-child probe for tests/test_scrubbed_env_coverage.py.

Not a test module (no test_ prefix, never discovered) and not under
.coveragerc's source dirs: what a traced child records for it does not
matter, only that the child STARTS coverage and writes a data file — the
observable that breaks when a scrubbed-env builder drops the COVERAGE_*
tracing keys.

The parent runs this probe with COVERAGE_PROCESS_START pointing at a
temp-dir rcfile, so the data file lands next to it. The probe imports
coverage itself (found via PYTHONPATH the test sets to the parent's own
site-packages) and calls process_startup(), mirroring what coverage's
installed a1_coverage.pth hook does at interpreter start. Doing it here
keeps the test layout-independent: on a pip install --user coverage the
scrubbed HOME of the module's env would hide the .pth from the child, and
that HOME-vs-install-location coupling is a separate finding (held PR
#73's covsite work), not the forwarding contract under test.
"""
import os
import sys

# The test passes the parent's coverage package dir via PYTHONPATH; the
# sys.path entry is enough to import it no matter where it is installed.
import coverage

cov = coverage.process_startup()
if cov is None:  # already started, or no env var requested it
    cov = getattr(coverage.process_startup, "coverage", None)
if cov is not None:
    cov.save()
print("probe ok")
