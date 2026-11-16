# Investigation skill v2 — design

Status: Implemented. The owner accepted this design 2026-11-05; [implementation-plan-v2.md](implementation-plan-v2.md) shipped it (phases P1–P7 done; see that plan's F-row decisions).

## Why a v2

The v1 skill (`skills-src/investigation/body.md`) is one fixed pipeline:
grounding → Nous v2 campaign in BLIS → `answer.json`. Three problems:

1. **It assumes Hive's lab will absorb this repo.** That's no longer happening.
   This repo is the permanent home for investigations, so `docs/hive-mapping.md`
   and the README's "Hive note" go.
2. **Every question must become a BLIS campaign.** If BLIS can't represent the
   question, v1 stops. Many good questions ("where does the EPP spend its time
   under our traffic?", "is prefix reuse on cluster X high enough to matter?")
   need code reasoning or live telemetry, not a simulator.
3. **Spotlights is only a grounding aid.** v1 mentions the static engine as an
   optional step 3 and ignores the telemetry path (`signal-pipeline`) entirely.

v2 keeps what worked — the answer contract, the validator, the gates, the
hash-locked apparatus for simulator campaigns — and turns the fixed pipeline
into a **planned** one: for each question the agent chooses which methods to
use (Spotlights, Nous, both, or neither), justifies the choice, and gets the
owner's approval before any spend.

## What stays the same

- **The answer contract** (`docs/answer-contract.md`), unchanged:
  `{answer, deliverable, repro, claims[{id, metric, value, condition, evidence}]}`,
  every evidence path must exist and contain the value.
- **`scripts/validate_answer.py`** stays the judge. It gets one optional
  extension (code citations, below); its existing semantics don't change.
- **The hard gates**: never launch without a go; engine state is read-only;
  show before writing into `final/`. Never change the question mid-study
  without the owner; report negative results as results.
- **The A1 apparatus discipline for Nous campaigns** — stays required (see
  "Nous path").
- **Studies committed, runs gitignored.**

## Where it runs and how it's packaged

### Runs inside the target repo

The owner opens an agent session **in the repo being investigated** (e.g.
`llm-d-router`, `vllm`) and invokes `/investigation`. That repo is the
subject of the study:

| Thing | v1 (repo then named ains-loop) | v2 |
|---|---|---|
| Target repo | named by absolute path | `git rev-parse --show-toplevel` of the session |
| Study folders | `ains-loop/studies/<slug>/` | `<target>/investigations/<slug>/` |
| Heavy engine output | `ains-loop/runs/` (gitignored) | outside the repo: `${INVESTIGATION_RUNS:-~/.cache/investigation}/<repo-name>/<slug>/` |
| Skill scripts | `ains-loop/scripts/` | bundled in the skill dir: `scripts/` (relative to SKILL.md) |

Consequences:
- **Pins come from the session repo.** `question.md` records the target's
  remote URL, branch, and `HEAD` sha at intake. Code citations default to that
  repo. Citing another repo (e.g. vLLM from an llm-d-router study) means
  naming its path and sha explicitly.
- **Spotlights** gets `--repo <toplevel>`. **Nous** gets
  `target_system.repo_path: <toplevel>` and
  `NOUS_CAMPAIGN_PARENT=$INVESTIGATION_RUNS/<repo>/<slug>`.
- **`investigations/`** is a normal visible folder. At the first study in a
  repo, `new_study.py` checks the remotes and asks the owner to pick a
  tracking mode, then records the answer in `investigations/config.yaml`:
  - **fork** (default): commit `investigations/` only on the owner's fork.
    The skill refuses to commit when the current branch's push remote is the
    upstream (org) remote, and never adds `investigations/` to an upstream PR.
  - **untracked**: add `investigations/` to `.git/info/exclude`, which is
    local and never committed (unlike `.gitignore`). Studies stay on disk only.

  In untracked mode, the per-repo `config.yaml` is untracked too, so it can
  move to `~/.config/investigation/repos/<repo>.yaml` instead.
- **Nothing in the skill is hard-coded to this laptop.** All the absolute
  `/Users/kelly/...` paths in v1 go away (see "Config").

### Config

There are two layers, so machine-specific paths never get committed into
someone else's repo:

- **`<target>/investigations/config.yaml`**, committed, per repo. It holds:
  - named clusters (kind `port-forward` or `url`, context/namespace/service or
    URL, the *name* of the token env var)
  - extra PromQL queries for this repo
  - `blis_applicable: true|false`
  - default target metric names
- **`~/.config/investigation/config.yaml`**, machine-local. It holds:
  - path to the BLIS apparatus root (still referenced by absolute path, never
    copied)
  - paths to the `nous`, `spotlights-engine`, `signal-pipeline`, and sim2real
    checkouts
  - the `INVESTIGATION_RUNS` default

`scripts/doctor.py` (read-only) runs first in every study. It reports which
tools and configs exist and therefore which methods are *available*. The
method plan can only pick available methods, and it lists the unavailable
ones with the reason (e.g. "nous-blis unavailable: no BLIS apparatus
configured").

### Packaging: Agent Skills spec

The skill follows the [Agent Skills specification](https://agentskills.io/specification):

```
skills/investigation/            ← name must match the folder
  SKILL.md                       frontmatter + core flow, < 500 lines / ~5k tokens
  scripts/
    doctor.py                    preflight: tools, configs, available methods
    capture_prom.py              Prometheus → frozen snapshot
    collect.py                   Nous v2 collector (moved from ains-loop/scripts)
    collect_spotlights.py
    collect_telemetry.py
    validate_answer.py           the contract judge (+ --check-citations)
    make_manifest.sh             A1 hash-lock template
    new_study.py                 scaffolds investigations/<slug>/ from assets/
  references/                    loaded only when the step needs it (one level deep)
    answer-contract.md
    method-plan.md               method menu, pushback rules, cost table
    telemetry.md                 sources, snapshot format, size guard
    spotlights.md                static + signal-pipeline usage, what to keep
    nous-blis.md                 v2 authoring facts, representability table, A1 apparatus
    sim2real.md                  algorithm vs BYO bundle, never deploy
    hand-scoring.md
  assets/
    question.md.tmpl  plan.md.tmpl  hand-score.md.tmpl  config.yaml.tmpl
    promql/starter.yaml
```

Frontmatter:

```yaml
---
name: investigation
description: >-
  Answer a one-off engineering question about the repo you're in (e.g. llm-d,
  vLLM) with evidence someone else can check. Plans which methods fit
  (code reading, a Prometheus telemetry snapshot, Spotlights static or
  telemetry-driven discovery, a Nous campaign in the BLIS simulator, sim2real
  prep), pushes back when none fit, and writes an answer.json where every
  number or code citation points at a file that provably contains it. Use when
  asked to investigate a question, check whether a shipped default still
  holds, explain where/why something happens under load, or answer an
  opportunities row. Never launches anything without the owner's explicit go.
license: Apache-2.0
compatibility: >-
  Designed for Claude Code or similar agents run from inside a git repo.
  Requires git and Python 3.10+ (scripts are stdlib-only). Optional per
  method: kubectl or network access to Prometheus; spotlights-engine and
  signal-pipeline; Nous v2 and a BLIS apparatus; a sim2real checkout.
metadata:
  version: "2.0.0"
  nous-pin: "nous-v2-fixes@e224cc1"
  spotlights-pin: "v0.1.1@cccc290"
---
```

**Pin policy.** Engines are pinned to the latest upstream *release*. As of
2026-11-05 that's Spotlights v0.1.1 @ `cccc290`, which is also `main`.
Unpushed local branches (e.g. `skill/spotlights-run` @ `e7aff25`) are never
pinned. `doctor.py` compares the installed engine with the pin and with the
latest upstream release, and warns if either differs. Each study's
`question.md` records the versions it actually ran with.

Rules that follow from the spec:
- `SKILL.md` keeps the gates, the 9 steps, and pointers. The detail for each
  step goes into `references/`, read only when that step runs.
- Scripts are called by their path relative to the skill directory. The skill
  could be installed in `.claude/skills/`, `.agents/skills/`, or a global
  dir, so SKILL.md tells the agent to resolve `scripts/…` against the folder
  that contains SKILL.md, and never against the target repo.
- `skills-ref validate skills/investigation` runs in CI.
- Skip `allowed-tools` (experimental, and support varies). The gates are
  written in the body.

### Install: `npx skills add`

From GitHub:

```bash
cd ~/git/llm-d-router
npx skills add AI-native-Systems-Research/groundwork --skill investigation -a claude-code   # → .claude/skills/investigation/
npx skills add AI-native-Systems-Research/groundwork --skill investigation -g               # → ~/.claude/skills/ (all repos)
```

The skills CLI looks in `skills/<name>/SKILL.md` (it walks container dirs up
to 3 levels deep), so the layout above is found without a manifest. To keep
it that way:
- There's no root `SKILL.md`, which would shadow everything below it.
- The existing `studies/` move to `examples/`. That isn't a searched container
  and has no SKILL.md, so it can't create ghost skills.
- `skill/build.sh` and `skills-src/` are deleted. The folder at
  `skills/investigation/` *is* the source. A generated copy would drift, and
  the installer reads whatever is committed.

To verify during implementation: that `skills add` copies the whole skill
directory (`scripts/`, `references/`, `assets/`), not just SKILL.md. The
check is to install from a local path into a throwaway git repo and run
`scripts/doctor.py` from the installed copy. That becomes a CI job.

### This repo's new role

The repo is published as `AI-native-Systems-Research/groundwork`.
groundwork becomes the **distribution repo** for the skill:

```
groundwork/
  skills/investigation/      the installable skill (above)
  tests/                     unit tests for the bundled scripts (not installed)
  examples/
    blocksize-sweep/         former studies/, kept as worked examples
    pd-threshold-retro/
  docs/                      design docs, build log (not installed)
  README.md                  what it is, install line, quickstart
  LICENSE
```

The two existing studies keep their content. Their evidence paths point at
the old layout, and a README note in `examples/` says so. They aren't rewritten.

## The flow

All study paths below are relative to `<target>/investigations/<slug>/`.
"Runs dir" means `$INVESTIGATION_RUNS/<repo>/<slug>/`, outside the repo.

```
0  Preflight         scripts/doctor.py → available methods (read-only)
1  Intake            question.md                       (agent drafts)
2  Method plan       plan.md                           ── GATE: owner approves plan
3  Telemetry         telemetry/<capture-id>/           (optional; capture = its own go)
4  Run methods       spotlights/, campaign.yaml        ── GATE: one go per launch
5  Collect           collection.json
6  Answer            final/answer.json                 ── GATE: owner sees draft first
7  Validate          validate_answer.py exits 0
8  sim2real prep     sim2real/                         (optional, Nous only; never deploys)
9  Hand-score        hand-score.md
```

### 1. Intake

As in v1: `question.md` has the question, where it came from, the falsifiable
form ("this is wrong if…"), and the baseline (usually the shipped default,
"B-ship"). Before drafting, grep earlier `investigations/` in this repo and Nous `principles.json`
files for prior learnings.

New: intake also records whether the question is **qualitative** ("where /
why / how does X work") or **quantitative** ("how much / does X beat Y"). That
decides which kinds of claims are allowed (see "Evidence kinds").

### 2. Method plan — the new core step

The agent writes `plan.md` and shows it to the owner. Nothing
costs money until the owner approves it.

`plan.md` contents:

| Section | What goes in it |
|---|---|
| Methods chosen | Any of: `code-reading`, `telemetry-snapshot`, `spotlights-static`, `spotlights-telemetry`, `nous-blis`, `sim2real-prep` |
| Why each, why not the others | One line per method, including the rejected ones |
| Order and hand-offs | e.g. telemetry → spotlights-telemetry finds the hot spot → nous-blis tests a fix |
| Cost and time | Rough $ and wall-clock per method |
| Claim kinds | Measured only, or measured + code citations |
| Pushback (if any) | Below |

**Method menu:**

| Method | Good for | Rough cost | Output |
|---|---|---|---|
| `code-reading` | "Where is X / what's the default" | ~free, minutes | citations |
| `telemetry-snapshot` | "What does cluster X actually do" | ~free, minutes | frozen metrics files |
| `spotlights-static` (`spotlights-engine --include <module> --objective …`) | Mechanism/policy questions in real code; "what would improve X" | ~$3–5, ~1 h per module | candidates + proposals with file:line |
| `spotlights-telemetry` (`signal-pipeline --telemetry-from …`) | Hot spots that only show up under load | ~$2, ~10 min | candidates tied to observed anomalies |
| `nous-blis` | Controlled "does X beat Y" over knobs BLIS represents | hours, GB of runs | measured claims |
| `sim2real-prep` | After a Nous win you might want to validate for real | minutes | transfer bundle (not deployed) |

**BLIS representability** (moved here from v1's step 2): if the question turns
on something BLIS can't represent (EPP scorer weights, speculative-entry TTL,
batch gateway, WVA autoscaling, RL time-slicing, wide-EP), the plan just
**excludes** `nous-blis` and says why. That no longer ends the study, and BLIS
is still never quietly approximated.

**Pushback.** The agent may conclude that *none* of the methods fit and say
so in `plan.md` rather than forcing one. Typical reasons:
- the question isn't falsifiable as posed → propose a re-posing;
- it needs data we can't get (e.g. traces when only metrics exist, or a
  cluster we don't have access to);
- it's already answered by a prior study → point at it;
- it's a pure lookup → answer it with `code-reading` alone and say that's all
  it needed.

A study may legitimately end at step 2 with a pushback. That's a result.

### 3. Telemetry

Metrics only (no OTel traces available). Every capture becomes a **frozen
snapshot** inside the study; a live endpoint is never cited as evidence,
because the contract requires evidence that someone can re-check later.

**Three sources**, chosen per study in `plan.md`:

| Source | How |
|---|---|
| `port-forward` | kube context + namespace + service; script runs `kubectl port-forward`, queries `localhost`, tears it down |
| `url` | a reachable Prometheus/Thanos URL; bearer token read from an env var named in the plan (never echoed, never written to disk) |
| `import` | an existing directory in the format `signal-pipeline` accepts — raw files (`metrics.jsonl`, `vllm_server.log`, …) or a pre-cooked `01_signals.json`; also sim2real's `metrics/raw/*_metrics.log` dumps. Copied in as-is |

**New script `scripts/capture_prom.py`** (stdlib only, like the validator):

```
capture_prom.py --study investigations/<slug> --capture-id <id>
                (--url URL [--token-env VAR] | --port-forward CTX/NS/SVC[:PORT])
                --start <rfc3339> --end <rfc3339> --step 30s
                [--queries assets/promql/starter.yaml] [--extra-queries FILE]
```

It writes `telemetry/<id>/` in the study:
- `metrics.jsonl` — one OTel-style `resourceMetrics` record per series sample
  batch, so `signal-pipeline` reads it with no adapter.
- `prom/<query-name>.json` — the raw `query_range` response per query (this is
  what claims cite).
- `capture.json` — source kind, endpoint (host only, no token), cluster/context,
  window, step, the exact query list, wall-clock of capture, and a sha256 of
  every file written.

The capture is read-only against the cluster, but it still touches an
external system, so it gets its own go.

**Choosing the window.** The window always depends on the question, so
`capture_prom.py` has no default; `--start`/`--end` are required. The skill
recommends a window in `plan.md` and the owner confirms or changes it at the
capture go. How it recommends:

| Question shape | Recommended window |
|---|---|
| Names an incident, deploy, or benchmark run | that event ± a margin (e.g. 15 min before → 15 min after); ask for the timestamps if they aren't given |
| "Before vs after" a change | two captures of equal length, one on each side of the change |
| Steady-state behavior ("what's our prefix hit rate") | the most recent 24 h, or 7 d if daily cycles matter |
| "Right now" | the last 1 h |

The step is picked so each query returns no more than ~11k points per series
(Prometheus's per-query limit), e.g. 30 s for ≤ 3 d. The plan states the
window in absolute RFC 3339 times, even when it was recommended as "the last
24 h", so the capture is reproducible. `capture.json` records the window that
was actually used.

Size guard: if a capture would exceed ~50 MB it goes in the runs dir
and the study keeps `capture.json` plus the per-query summaries that the claims
actually cite.

**Starter PromQL list** — `assets/promql/starter.yaml`, merged with the repo's `investigations/config.yaml` extras. Metric names were
checked against vLLM @ `2f1cba799e` (`vllm/v1/metrics/`) and llm-d-router @
`618eb0a9`. For now every study uses the same list; studies can append
`--extra-queries`. Picking queries per question is future work.

```yaml
# vLLM — load and queueing
vllm_running:          sum by (pod) (vllm:num_requests_running)
vllm_waiting:          sum by (pod) (vllm:num_requests_waiting)
vllm_preemptions_rate: sum by (pod) (rate(vllm:num_preemptions[5m]))
# vLLM — KV cache and prefix reuse
vllm_kv_usage:         avg by (pod) (vllm:kv_cache_usage_perc)
vllm_prefix_hit_ratio: sum by (pod) (rate(vllm:prefix_cache_hits[5m])) / sum by (pod) (rate(vllm:prefix_cache_queries[5m]))
vllm_ext_prefix_hit_ratio: sum (rate(vllm:external_prefix_cache_hits[5m])) / sum (rate(vllm:external_prefix_cache_queries[5m]))
# vLLM — latency (p50/p99 from histograms)
vllm_ttft_p50:  histogram_quantile(0.5,  sum by (le) (rate(vllm:time_to_first_token_seconds_bucket[5m])))
vllm_ttft_p99:  histogram_quantile(0.99, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket[5m])))
vllm_itl_p50:   histogram_quantile(0.5,  sum by (le) (rate(vllm:inter_token_latency_seconds_bucket[5m])))
vllm_itl_p99:   histogram_quantile(0.99, sum by (le) (rate(vllm:inter_token_latency_seconds_bucket[5m])))
vllm_queue_p99: histogram_quantile(0.99, sum by (le) (rate(vllm:request_queue_time_seconds_bucket[5m])))
vllm_e2e_p99:   histogram_quantile(0.99, sum by (le) (rate(vllm:e2e_request_latency_seconds_bucket[5m])))
# vLLM — throughput and shape
vllm_prompt_tps:     sum (rate(vllm:prompt_tokens_total[5m]))
vllm_gen_tps:        sum (rate(vllm:generation_tokens_total[5m]))
vllm_prompt_len_p50: histogram_quantile(0.5, sum by (le) (rate(vllm:request_prompt_tokens_bucket[5m])))
vllm_gen_len_p50:    histogram_quantile(0.5, sum by (le) (rate(vllm:request_generation_tokens_bucket[5m])))
# llm-d EPP — routing and scheduling
epp_request_rate:     sum (rate(llm_d_epp_request_total[5m]))
epp_error_rate:       sum (rate(llm_d_epp_request_error_total[5m]))
epp_e2e_p99:          histogram_quantile(0.99, sum by (le) (rate(llm_d_epp_request_duration_seconds_bucket[5m])))
epp_ntpot_p99:        histogram_quantile(0.99, sum by (le) (rate(llm_d_epp_request_ntpot_seconds_bucket[5m])))
epp_plugin_p99:       histogram_quantile(0.99, sum by (le, plugin_type) (rate(llm_d_epp_plugin_duration_seconds_bucket[5m])))
epp_sched_attempts:   sum by (status) (rate(llm_d_epp_scheduler_attempts_total[5m]))
epp_cached_tokens_p50: histogram_quantile(0.5, sum by (le) (rate(llm_d_epp_request_cached_tokens_bucket[5m])))
epp_kv_util_avg:      llm_d_epp_average_kv_cache_utilization
epp_kv_util_stddev:   llm_d_epp_std_dev_kv_cache_utilization
epp_queue_per_endpoint: llm_d_epp_per_endpoint_queue_size
epp_disagg_decisions: sum by (decision) (rate(llm_d_epp_disagg_decision_total[5m]))
epp_flow_queue:       sum (llm_d_epp_flow_control_queue_size)
```

To check during implementation: histogram `_bucket` suffixes and label names
(`plugin_type`, `status`, `decision`) are inferred from the metric names and
must be confirmed against the source or a live `/metrics`. The capture records
any query that comes back empty, rather than failing.

### 4. Run methods

Each launch (Spotlights engine run, `signal-pipeline` run, Nous campaign)
gets its own go, after a 2–3 sentence summary of what runs and what it costs.
Approving the plan doesn't count as approving the launches.

**Spotlights paths.** Outputs go to `<runs dir>/spotlights-<id>/`
(gitignored). The study keeps the parts it cites: `spotlights/sorted_candidates.md`,
`spotlights/result.json` (the `SpotlightReport`), and the run manifest
(engine version, target repo + commit, objective, hints, cost).
- `spotlights-static`: `/spotlights-objective-setting` → `spotlights-engine`
  (via `/spotlights-run`) → `/spotlights-sort-candidates`. Reuse an existing
  module output when one covers the question.
- `spotlights-telemetry`: `signal-pipeline --telemetry-from investigations/<slug>/telemetry/<id>/ --to-stage 04`.
  Stage 05 changes the repo and is **never** run by this skill.
- With metrics only, `signal-pipeline`'s anomaly extraction has less to go on
  than its trace-based canonical runs. `plan.md` should say so. Expect fewer,
  coarser candidates (pod/metric level, not function level). Static Spotlights
  on the module the metrics point at fills that gap.

**Nous path (`nous-blis`).** Same as v1 steps 4–7, including the **required**
A1-style apparatus: per-study `scripts/<slug>/` at the BLIS apparatus root
(arm definitions, idempotent driver with binary-sha check, pre-registered
analysis script), hash-locked with `make_manifest.sh`, manifest sha recorded in
the campaign's `locked_parameters`, and the pre-launch checks.

New: telemetry can **calibrate** a BLIS workload, e.g. arrival rate, prompt
and generation length distributions, and prefix-hit ratio from a snapshot. If
so, the workload derivation script is part of the hash-locked apparatus. The
claims cite the snapshot file the parameters came from, so "simulated on
traffic like cluster X's" is checkable.

**Chaining.** When the plan chains methods (telemetry → Spotlights → Nous),
each hand-off is a file in the study: the Nous campaign's
`target_system.description` cites the Spotlights candidate id and the
telemetry capture id it builds on.

### 5. Collect

One collector per method. All of them are read-only over the runs dir, all write
into `collection.json` under a per-method key, and all draft claims with
verified flags.
- `nous`: existing `scripts/collect.py`.
- `spotlights`: new `scripts/collect_spotlights.py`. It reads `result.json`
  and drafts code-citation claims for the top candidates (see below).
- `telemetry`: new `scripts/collect_telemetry.py`. It reduces the per-query
  responses to summary numbers (mean/p50/max over the window) and writes them
  to `telemetry/<id>/summary.json`, which measured claims can cite.

### 6–7. Answer and validate — evidence kinds

The claim shape stays the same. What changes is what an evidence file can be:

| Kind | Evidence file | Allowed when |
|---|---|---|
| **measured** (sim) | Nous work-dir artifacts copied into the study, as today | always |
| **measured** (telemetry) | `telemetry/<id>/summary.json` or `prom/<q>.json` | always; `condition` must name cluster, window, query |
| **code citation** | `citations/<claim-id>.txt` | qualitative questions only (set at intake) |

A **code citation** keeps the contract's five keys:

```json
{"id": "c3", "metric": "prefix-scorer block hashing site",
 "value": "func (db *ChunkedTokenDatabase) TokensToKVBlockKeys",
 "condition": "llm-d-router @ 618eb0a9, pkg/kvcache/kvblock/token_processor.go:L88",
 "evidence": "citations/c3.txt"}
```

`citations/c3.txt` has a header line `repo@sha:path:Lstart-Lend` and then
the quoted lines, verbatim. The existing validator already checks that the
`value` string is in the file. New optional flag
`validate_answer.py --check-citations` reads the header, runs
`git -C <repo> show <sha>:<path>`, and checks that the quoted lines really are
at those line numbers at that commit. That catches citations that are
fabricated or have gone stale, and default behavior doesn't change.

Spotlights *proposals* ("changing X would reduce Y") are hypotheses, not
claims. An answer may describe them in prose, labelled as proposals, but a
number Spotlights estimated never becomes a claim value. Only measured numbers
do.

### 8. sim2real prep (optional, Nous only)

When a Nous campaign finds a winner and the owner wants a path to a real
validation, the skill prepares (and **never deploys**) a sim2real input under
`sim2real/` in the study:

- **Algorithm campaigns** (Nous evolved a scorer/decider in Go): write the
  BLIS-mode bootstrap folder — `algorithms/*.go` from the winning arm,
  `workloads/*.yaml` from the campaign's workloads, `config.md` from the
  locked arms. Next step for the owner: `/sim2real-bootstrap` in that folder.
- **Knob campaigns** (blocksize, P/D threshold — no new code): write a
  `--byo`-mode overlay — shipped EPP image + the winning flag/config as a
  treatment overlay against the B-ship baseline — and the printed
  `sim2real translation register` command.

Deploying (`deploy.py`, Tekton on a cluster) is out of scope and stays a
manual, owner-run step. If the owner runs it, sim2real's
`metrics/raw/*_metrics.log` and `metrics/processed/metrics_summary.json` come
back through telemetry `import`, and a **follow-up study** can compare BLIS's
prediction with what the cluster measured, using the same contract.

### 9. Hand-score

As in v1. `docs/hand-scoring.md` gets one new item: "was the method plan
right in hindsight?" (did we over- or under-spend, should pushback have
happened).

## Study layout (v2)

```
<target>/investigations/
  config.yaml          per-repo: clusters, extra PromQL, blis_applicable
  <slug>/
    question.md        intake (+ qualitative/quantitative, target repo sha)
    plan.md            methods, rationale, cost, pushback       NEW
    grounding.md       code-reading notes (as v1)
    telemetry/<id>/    capture.json, metrics.jsonl, prom/, summary.json   NEW
    spotlights/        sorted_candidates.md, result.json, manifest        NEW
    citations/         <claim-id>.txt                            NEW
    campaign.yaml      if nous-blis
    collection.json    per-method sections
    sim2real/          if sim2real-prep                          NEW
    final/answer.json
    hand-score.md

$INVESTIGATION_RUNS/<repo>/<slug>/      outside the repo, never committed
  nous/  spotlights-<id>/  large telemetry captures
```

## Work to implement

1. **Restructure the repo**: create `skills/investigation/` in the spec
   layout. `git mv` `scripts/*` into it, and `docs/answer-contract.md` +
   `docs/hand-scoring.md` into `references/`. Delete `skill/build.sh` and
   `skills-src/`. Move `studies/` to `examples/`. Update `tests/` imports.
2. **Write SKILL.md** (core flow + gates, under 500 lines) and the
   `references/` file for each step. Remove every `/Users/kelly/...` path;
   use config lookups instead.
3. `scripts/doctor.py` + `scripts/new_study.py` + `assets/` templates.
4. `scripts/capture_prom.py` + `assets/promql/starter.yaml` + tests with a
   recorded Prometheus response fixture.
5. `scripts/collect_spotlights.py`, `scripts/collect_telemetry.py` + tests.
   `collect.py` takes the runs dir from config.
6. `validate_answer.py --check-citations` + tests. Existing tests must pass
   unchanged.
7. `references/answer-contract.md`: add the evidence-kinds section without
   touching the verbatim contract. `references/hand-scoring.md`: add the
   method-plan item.
8. Delete `docs/hive-mapping.md`. Rewrite the README (what it is, the
   `npx skills add` line, quickstart, credits incl. sim2real + telemetry).
   Add a LICENSE.
9. **CI**: unit tests; `skills-ref validate skills/investigation`; an install
   smoke test (`npx skills add ./ --skill investigation` into a temp git
   repo, then run the installed `scripts/doctor.py`).

## Open questions

- **Per-question query selection** (future): have the agent pick or extend
  PromQL from the question and the Spotlights module, instead of always using
  the starter list.
