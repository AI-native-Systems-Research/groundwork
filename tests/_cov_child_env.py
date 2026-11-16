"""Shared child-env keys that keep subprocess coverage working.

The env-isolation tests build a scrubbed env for their python children
(fake HOME, PATH holding only git). Those children still need to be
measured when the suite runs under ``coverage run``: the parent exports
``COVERAGE_PROCESS_START``/``COVERAGE_PROCESS_CONFIG`` (.coveragerc sets
``patch = subprocess``) and the tests forward every ``COVERAGE_*`` key.
That forwards the *config*, but not the *import*: on a ``pip install
--user`` coverage the child finds coverage (and its ``a1_coverage.pth``
startup hook) through the user site-packages, which are resolved via
``$HOME`` — scrubbed along with everything else. The child then runs
unmeasured, and a file exercised only by such tests (new_study.py) reads
0% locally while CI's system-site coverage reads 85%.

``covsite_env()`` adds the two keys that close the gap without un-scrubbing
anything: ``PYTHONPATH`` pointing at ``tests/_covsite`` (its
``sitecustomize`` runs ``coverage.process_startup()`` in the child), and
``COVERAGE_SITE`` naming the parent's site-packages so the child can
import coverage at all. Both keys are inert when the suite is not run
under coverage (the hook checks ``COVERAGE_PROCESS_START`` /
``COVERAGE_PROCESS_CONFIG`` first), and harmless when coverage lives in
the system site (``process_startup`` is idempotent).
"""
import os
import sys
from pathlib import Path

_COVSITE = Path(__file__).resolve().parent / "_covsite"


def covsite_env() -> dict:
    """Env keys a scrubbed child env needs to stay measurable."""
    keys: dict[str, str] = {}
    if os.environ.get("COVERAGE_PROCESS_START") or \
            os.environ.get("COVERAGE_PROCESS_CONFIG"):
        # Do not clobber a PYTHONPATH the caller set for its own reasons;
        # append ours after it.
        existing = os.environ.get("PYTHONPATH", "")
        parts = [p for p in existing.split(os.pathsep) if p]
        ours = str(_COVSITE)
        if ours not in parts:
            parts.append(ours)
        keys["PYTHONPATH"] = os.pathsep.join(parts)
        try:
            import coverage
        except ImportError:
            pass  # the hook is inert without an importable coverage anyway
        else:
            site_dir = os.path.dirname(os.path.dirname(coverage.__file__))
            if site_dir not in sys.path:
                sys.path.append(site_dir)
            keys["COVERAGE_SITE"] = site_dir
    return keys
