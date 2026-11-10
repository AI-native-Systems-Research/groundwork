# P/D threshold retro — the question

## The question

> Does a fitted per-layout rule for the P/D disaggregation threshold beat the shipped
> default T=16?

This is a **retrospective** study: the question was already answered by the A1 campaign
(`llmd-a1-pd-threshold`, finished 2026-10-16). This study re-answers it through the
`investigation` skill's artifact contract, using A1's real files as evidence — no new
simulator runs, no model spend. It exists to exercise every artifact contract
(question, grounding, answer, validation, hand-scoring) with real data, and it stays in
the repo as the reference example.

## Where it came from

The A1 campaign brief at
`/Users/kelly/git/nous-run/nous-campaigns/blis-pd-decider-threshold/campaign-a1.yaml`
(`research_question` field). The shipped default is the prefix-threshold decider at
T=16 in llm-d-router.

## Falsifiable form (pre-registered by A1)

A fitted rule beats the shipped T=16 if its **gated gain** — the median capacity gain
over held-out win cells, granted only when every guardrail, control, and held-out-win
check passes — is **≥ 5%**. It fails if any guardrail breaks (regret, no-loss,
attainment, fail-fraction), any control check fails (decision equivalence), or no
held-out cell clears the ≥5% win bar.

## Baseline

B-ship = the shipped default, prefix-threshold decider at T=16 (arm `t16` in the A1
grid). Every fitted-rule arm is judged against it on the same workload, layout,
bandwidth, and seed.

## Apparatus pins (A1, verified during the campaign)

| Pin | Value |
|---|---|
| Campaign root | `/Users/kelly/git/nous-run/nous-campaigns/blis-pd-decider-threshold` |
| Work dir | `/Users/kelly/git/nous-run/work/llmd-a1-pd-threshold` (phase DONE, iteration 2) |
| BLIS binary | `bin/blis-9addaae2`, sha256 `43dbef622ece2aef6034bad9f701352db4451a81326db2564361add449d5eb4d` |
| BLIS catalog commit | `6fe4664576bc43d601bba6619aca4fbb42c5087b` |
| Apparatus manifest | `scripts/a1/MANIFEST.sha256`, sha256 `b1571e4693963431fec50e5c5880b99ff6c26f1b8cf41206b32699f41dcfe11d` |
| Analysis script (locked) | `scripts/a1/capacity.py` (verdict metric: `a1_guidance_gated_gain_pct`) |

## Collection note

The A1 work dir is **pre-v2 Nous** (v0.5.0: `best_found.json`, `meta_findings.json`,
no `pre_work.json`). `scripts/collect.py` reads v2 work dirs only and refuses this one
by design, so this study's claims were assembled by hand from the A1 artifacts —
documented here, and visible in that `collection.json` is absent from this study. A
future v2 campaign produces that file automatically.

One correction to the spec's worked example, found by pointing the validator at the
real files: `best_found.json` does **not** contain the −100.0 verdict (its top_k
components are all 0.0); the verdict number lives in
`runs/iter-2/results/a1/analysis/objective_metadata.json`. The claims below cite the
files that actually contain their values.
