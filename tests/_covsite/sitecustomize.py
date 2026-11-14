"""Child-side subprocess-coverage bootstrap; imported via PYTHONPATH.

Coverage's subprocess support needs two things in every python child: an
importable ``coverage`` module, and a call to ``coverage.process_startup()``
at interpreter start. Both normally come from the ``.pth`` file a coverage
wheel installs into site-packages (``a1_coverage.pth``). When coverage is a
``pip install --user`` install, that file lives in the *user*
site-packages, which a child resolves through ``$HOME`` — so any test env
that scrubs HOME (the env-isolation tests: new_study, doctor, runtime_*)
silently loses every child's measurement on such machines while CI
(coverage in the system/venv site) measures the same children fine. That
split once read new_study.py at 0% locally and 85% in CI for the same
commit, failing ``scripts/ci-local.sh unit`` on the per-file floor while
GitHub's unit job stayed green.

Directory entries on PYTHONPATH are not scanned for ``.pth`` files, but
``sitecustomize`` is imported from anywhere on ``sys.path``, so the env
builders put this directory on the child's PYTHONPATH. The logic mirrors
``a1_coverage.pth`` exactly, with one addition: when ``coverage`` cannot
be imported, the directory named by ``COVERAGE_SITE`` (the test parent's
own site-packages) is appended first.
"""
import os
import sys

if os.getenv("COVERAGE_PROCESS_START") or os.getenv("COVERAGE_PROCESS_CONFIG"):
    try:
        import coverage
    except ImportError:
        site_dir = os.getenv("COVERAGE_SITE")
        if site_dir and site_dir not in sys.path:
            sys.path.append(site_dir)
        try:
            import coverage  # noqa: F811
        except ImportError:
            coverage = None  # type: ignore[assignment]
    try:
        coverage.process_startup()
    except (AttributeError, NameError):
        pass
