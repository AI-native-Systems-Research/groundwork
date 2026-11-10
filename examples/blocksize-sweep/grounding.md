# Block-size sweep — grounding

## Skip reason

This is a numeric sweep over a documented knob, not a question about a mechanism or policy hidden in
the code. Grep-level grounding is the honest choice here. A Spotlights run would add cost (roughly
$3–5 and about an hour) without changing what gets swept — the lever, its default, and its config
path are all visible with plain grep.

## Where the lever lives

All citations verified read-only in `/Users/kelly/git/llm-d-router`.

**The precise path — static 16:**

- `pkg/kvcache/kvblock/token_processor.go:32-34` — `defaultBlockSize = 16`, with the comment "16 is
  the default value used by vLLM". This is the constant the precise path resolves to.
- `pkg/kvcache/kvblock/token_processor.go:49-55` — the config struct: `BlockSize` (deprecated,
  `json:"blockSize"`) and `BlockSizeTokens` (`json:"blockSizeTokens"`). A zero value means "not set"
  and resolves to the default (16).
- `pkg/kvcache/kvblock/token_processor.go:121-127` — the resolution logic: zero `BlockSizeTokens`
  becomes 16; a set deprecated `BlockSize` is promoted to `BlockSizeTokens`; values ≤ 0 are rejected.
- `pkg/epp/framework/plugins/requestcontrol/dataproducer/preciseprefixcache/producer.go:216` — the
  precise producer takes its block size from `tokenProcessor.BlockSize()`.
- `pkg/epp/framework/plugins/requestcontrol/dataproducer/preciseprefixcache/producer.go:327` — block
  keys are computed with that `blockSizeTokens` per request.
- Config files that set it explicitly: `deploy/config/sim-pd-epp-config.yaml:8` (`blockSizeTokens: 16`,
  under the approx-prefix-cache-producer plugin with `autoTune: false` — a sim tuning sample) and
  `test/perf/config/router-configs/precise-prefix.yaml:78` (`blockSizeTokens: 16`).

**The approximate path — auto-tune with a 64-token floor:**

- `pkg/epp/framework/plugins/requestcontrol/dataproducer/approximateprefix/types.go:99-100` —
  `defaultBlockSizeTokens = 16` (vLLM default) as the config default.
- `pkg/epp/framework/plugins/requestcontrol/dataproducer/approximateprefix/types.go:139-141` — the
  `AutoTune` flag and `BlockSizeTokens` config field.
- `pkg/epp/framework/plugins/requestcontrol/dataproducer/approximateprefix/types.go:159-161` —
  defaults: `AutoTune: true`, `BlockSizeTokens: 16`.
- `pkg/epp/framework/plugins/requestcontrol/dataproducer/approximateprefix/plugin.go:44-52` —
  `minBlockSizeTokens = 64`, the floor; the comment explains the routing-side indexer keeps one LRU
  entry per (pod, block), so small blocks blow up indexer memory.
- `pkg/epp/framework/plugins/requestcontrol/dataproducer/approximateprefix/plugin.go:334-357` —
  `GetBlockSize(endpoints)`: when `AutoTune` is on, it prefers the endpoint metric
  `CachePrefixMatchUnit`, falls back to `CacheBlockSize`, and clamps anything below 64 up to 64 —
  including values that came from explicit config.

## What the shipped default is

- **Precise path: 16**, static and configurable (`blockSizeTokens` in the token processor config).
- **Approximate path: auto-tuned** from endpoint metrics, defaulting on (`AutoTune: true`), clamped to
  a **64-token floor**.

## The gap

No block-size sensitivity sweep exists in `llm-d-benchmark` — that is what the opportunities row says
and what this study fills. The lever is fully configurable on both paths; what is missing is any
measurement of how sensitive cache capacity and hit rate are to the value, per workload.

## Prior learnings

Grepped `/Users/kelly/git/nous-run/work/*/principles.json` (read-only) for block-size-related
principles. **No principle about block-size tuning itself — none found.**

Two adjacent hits mention `blockSize` as a variable inside the PD-decider formula, not as a tuning
lever:

- `work/llmd-a1-pd-threshold/principles.json` — notes that
  `nonCachedTokens = inputTokens - cachedBlocks*blockSize` in `sim/disaggregation.go:174-175`, so the
  decider's behavior depends on block size as an input.
- `work/pd-decider-p3b/principles.json` — same formula (`sim/disaggregation.go:111-178`), used to
  explain a threshold-collapse failure mode.

Worth remembering during the sweep: block size feeds the PD decider's arithmetic, so changing it can
move disaggregation decisions too, not just hit rates.
