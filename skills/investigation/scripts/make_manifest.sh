#!/usr/bin/env bash
# Regenerate scripts/a1/MANIFEST.sha256 (paths relative to the campaign root). Check with:
#   cd <campaign root> && shasum -a 256 -c scripts/a1/MANIFEST.sha256
# Covers the binary, env.sh, the A1 scripts, the trace converter, the raw traces, the per-seed trace
# samples (evaluation seeds 1-3 and the pilot/rehearsal seed 42) and the calibration gate's inputs.
# Load-stage files (traces/a1/load/) are not listed: a1.trace_stage derives them from the samples.
set -euo pipefail
cd "$(dirname "$0")/../.."
{
  echo bin/blis-9addaae2
  echo env.sh
  for f in a1.py driver.py capacity.py wait.sh make_manifest.sh; do echo scripts/a1/$f; done
  echo scripts/replay.sh
  echo scripts/p3b/calib_gate.py
  echo repo/tools/hashtrace_to_tracev2.py
  echo repo/tools/predict_reuse.py
  echo repo/defaults.yaml
  echo repo/hardware_config.json
  for f in toolagent_trace.jsonl conversation_trace.jsonl weka_with_subagents_051926.jsonl; do echo traces/$f; done
  for t in toolagent conversation weka; do
    for s in 1 2 3 42; do
      for x in yaml csv hashes.jsonl; do echo traces/a1/${t}_s$s.$x; done
    done
  done
  for t in toolagent_w600 conversation_w600; do
    for x in yaml csv hashes.jsonl; do echo traces/blis2/$t.$x; done
  done
} | while IFS= read -r f; do shasum -a 256 "$f"; done > scripts/a1/MANIFEST.sha256
wc -l < scripts/a1/MANIFEST.sha256
shasum -a 256 scripts/a1/MANIFEST.sha256
