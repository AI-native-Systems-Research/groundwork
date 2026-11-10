# Block-size sweep — the question

## The question

> llm-d chunks the prefix cache into fixed 16-token blocks. Does 16 still hold across our workload set, and where does it break?

This is row 9 of the llm-d opportunities list — see the "Prefix cache block size" row in
[/Users/kelly/git/hw2/llm-d/opportunities.md](/Users/kelly/git/hw2/llm-d/opportunities.md).

## Where it came from

The opportunities table classifies this row as needing re-scoping. Here is why, in plain terms:

- The **approximate path** already auto-tunes. `llm-d-router` picks `blockSizeTokens` per model from
  vLLM's `prefix_match_unit` / `block_size` metrics (`autoTune` defaults to true), and the result is
  clamped to a 64-token floor so the LRU indexer's memory stays bounded.
- The **precise path** still uses a static, configurable 16.
- **No block-size sensitivity sweep exists in `llm-d-benchmark`.** That is the gap this study fills.

So this study is the missing measurement — a sweep of one documented knob — not new auto-tuning. The
auto-tuning proposal (llm-d-router#2873) is separate live work; we are not building it here.

## Falsifiable form (pre-registered)

**Claim: 16 is the right default block size for the precise path.**

- The claim **stands** if, for every workload in our set (W1–W4, C0, W5t/W5w, W6) and every alternative
  block size in {8, 32, 64}, the alternative does **not** improve the headline capacity-or-hit-rate
  metric by **5% or more** (averaged over seeds 1–3, compared against the B-ship arm on the same
  workload and layout), or does improve it but breaks the latency stage gates (TTFT/TPOT).
- The claim is **wrong** if some alternative in {8, 32, 64} improves capacity-or-hit-rate by **≥5%**
  on at least one workload, averaged over seeds 1–3, while staying inside the TTFT/TPOT stage gates.

The 5% threshold is explicit and fixed up front. "Capacity-or-hit-rate" means whichever headline
capacity metric the A1 scoring already computes for that workload; the metric and direction get locked
in the campaign.yaml before any run.

## Baseline

**B-ship** = the shipped default, block size **16**. It is the control arm in every comparison. Every
alternative arm is only ever judged against B-ship on the same workload, layout, and seed.

## Apparatus pins (verified 2026-11-03)

| Pin | Value |
|---|---|
| BLIS binary | `/Users/kelly/git/nous-run/nous-campaigns/blis-pd-decider-threshold/bin/blis-9addaae2` |
| BLIS binary sha256 | `43dbef622ece2aef6034bad9f701352db4451a81326db2564361add449d5eb4d` (verified with `shasum -a 256`) |
| BLIS catalog | git repo at `/Users/kelly/git/nous-run/nous-campaigns/blis-pd-decider-threshold/blis-catalog` |
| BLIS catalog commit | `6fe4664576bc43d601bba6619aca4fbb42c5087b` (verified read-only with `git rev-parse HEAD`) |
| A1 apparatus manifest | `scripts/a1/MANIFEST.sha256` under the campaign root |
| A1 manifest sha256 | `b1571e4693963431fec50e5c5880b99ff6c26f1b8cf41206b32699f41dcfe11d` (verified with `shasum -a 256` of the manifest file) |

Note: when the block-size driver scripts are written (a later session, at the apparatus under
`scripts/blocksize/`), they get their **own** `MANIFEST.sha256`. At that point this file records the
new pins.

## Planned sweep

- `--block-size-in-tokens` ∈ **{8, 16, 32, 64}**; 16 = B-ship control.
- `--hash-chain-block-tokens` held at the trace-appropriate value: **64 for the Weka trace, 512 for
  the others** — this matches how the A1 scripts pass it today.
- Seeds **1–3**.
- The A1 workload/layout set: **W1–W4** synthetic, **C0**, **W5t/W5w/W6** traces; layouts **2P/6D,
  3P/5D, 4P/4D** plus **agg8**.

What the A1 scripts actually do today (checked read-only in
`/Users/kelly/git/nous-run/nous-campaigns/blis-pd-decider-threshold/scripts/a1/a1.py`, lines 238–240):
`--hash-chain-block-tokens` is passed as `64` for the weka trace and `512` for every other trace, and
`--block-size-in-tokens 64` is passed **only for the weka trace**. Every other workload leaves the
flag unset and runs at BLIS's built-in default. Two consequences for this sweep:

1. The sweep must pass `--block-size-in-tokens` explicitly on **every** run so the arm is always
   pinned, never inherited.
2. **Decision (Kelly, 2026-11-05): B-ship = 16 uniformly, including weka.** This directly answers
   the study question ("does the shipped default hold?"), and the 64 arm on weka doubles as a test
   of whether A1's block-size-64 choice there actually mattered. The weka sidecar's 64-token hash
   blocks are a property of the trace and stay at `--hash-chain-block-tokens 64`; only the cache
   block size moves.

## Campaign note

The `campaign.yaml` will be authored per **Nous v2** (branch `origin/nous-v2-fixes` of
`~/git/agentic-strategy-evolution`): objective as `{metric, direction}`, domain context in
`target_system.description`, and `locked_parameters` enforced so nothing but the block size moves
between arms. It is **not yet written** — this study is at the setup stage.

## Known limits

These must appear in the eventual answer, stated plainly:

- **BLIS cannot represent the approximate-prefix LRU or `maxPrefixTokensToMatch`.** The sweep speaks
  only about the precise block-size axis. It says nothing about the approximate path's auto-tuning,
  its 64-token floor, or its per-model behavior.
