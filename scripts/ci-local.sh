#!/usr/bin/env bash
# Run the CI jobs from .github/workflows/ci.yml locally, with the local python.
# Not installed with the skill. Usage: scripts/ci-local.sh [job ...]
#   jobs: unit spec install-smoke no-hardcoded-paths   (default: all four)
# Prints PASS/FAIL per job; exits nonzero if any job fails.
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-python3}"
LOG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ci-local.XXXXXX")"
JOBS=("$@")
[ ${#JOBS[@]} -eq 0 ] && JOBS=(unit spec install-smoke no-hardcoded-paths)

job_unit() {
  "$PY" -c 'import sys; assert sys.version_info >= (3, 10), sys.version' || return 1
  "$PY" -c 'import yaml' || { echo "PyYAML missing: $PY -m pip install pyyaml"; return 1; }
  # CI gates on coverage (.coveragerc). Mirror that when coverage is installed;
  # fall back to a bare run so the job still works without it.
  if ! "$PY" -c 'import coverage' 2>/dev/null; then
    echo "coverage missing ($PY -m pip install coverage): running without the coverage gate"
    (cd "$REPO" && "$PY" -m unittest discover tests)
    return $?
  fi
  (
    cd "$REPO" || exit 1
    export COVERAGE_PROCESS_START="$REPO/.coveragerc"
    "$PY" -m coverage erase
    "$PY" -m coverage run -m unittest discover tests || exit 1
    "$PY" -m coverage combine || exit 1
    "$PY" -m coverage report
    # Per-file floor: a total-only gate stays green when one file drops to 0%
    # (issues #2/#3/#8). ci-local.sh is currently the only gate for the
    # floor; the CI unit job's step is pending issue #70.
    "$PY" "$REPO/scripts/check_coverage_floor.py"
  )
}

job_spec() {
  "$PY" "$REPO/scripts/check_skill_spec.py" "$REPO/skills/investigation" || return 1
  # Official reference validator (PyPI skills-ref, needs Python 3.11+; its
  # console script is `agentskills`, newer source names it `skills-ref`).
  local v
  for v in skills-ref agentskills; do
    if command -v "$v" >/dev/null 2>&1; then
      "$v" validate "$REPO/skills/investigation"; return $?
    fi
  done
  echo "note: skills-ref not installed (pip install skills-ref); ran check_skill_spec.py only"
}

job_install_smoke() {
  command -v npx >/dev/null 2>&1 || { echo "npx not found"; return 1; }
  local tmp; tmp="$(mktemp -d "${TMPDIR:-/tmp}/install-smoke.XXXXXX")"
  (
    set -e
    cd "$tmp"
    git init -q
    npx -y skills add "$REPO" --skill investigation -a claude-code -y
    for d in scripts references assets; do
      test -d ".claude/skills/investigation/$d" || { echo "missing .claude/skills/investigation/$d"; exit 1; }
    done
    "$PY" .claude/skills/investigation/scripts/doctor.py --json --offline >/dev/null
    # R9: submit + prepare through the INSTALLED runtime with the test-only
    # fake adapter (tests/, never shipped): no checkout paths, no Hive/Crux,
    # nothing approved or launched.
    "$PY" "$REPO/tests/_runtime_install_smoke.py" .claude/skills/investigation "$tmp"
  )
  local rc=$?
  rm -rf "$tmp"
  return $rc
}

job_no_hardcoded_paths() {
  if grep -rn --exclude-dir=__pycache__ /Users/ "$REPO/skills/"; then
    echo "hardcoded /Users/ paths found under skills/"; return 1
  fi
  return 0
}

fail=0
for job in "${JOBS[@]}"; do
  fn="job_${job//-/_}"
  if ! declare -F "$fn" >/dev/null; then echo "unknown job: $job"; fail=1; continue; fi
  log="$LOG_DIR/$job.log"
  if "$fn" >"$log" 2>&1; then
    echo "PASS $job"
  else
    echo "FAIL $job  (log: $log)"
    tail -n 25 "$log" | sed 's/^/    /'
    fail=1
  fi
done
exit $fail
