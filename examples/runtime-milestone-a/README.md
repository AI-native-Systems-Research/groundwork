# Milestone A real runs (plan R9)

R9 needs two real runs through the runtime, each with the owner's own go.
The third run (Spotlights telemetry-driven) stays **open**: owner decision
D10 holds R6a/R6 until the upstream Spotlights stage 03 prompt handles
metrics-only workloads.

Nothing here has been approved or launched. There are no request files to
fill in. You talk to the skill in plain language. It writes the request,
derives every structured field (commit, apparatus locks and sha256, module
names, run ids) and shows you an approval card to review.

## Setup (once)

Install the skill globally, so the target repo stays clean (`prepare`
refuses a dirty tree; a project install adds untracked `.claude/skills/` or
`.agents/skills/`):

```bash
npx skills add /path/to/groundwork --skill investigation -g              # Claude Code → ~/.claude/skills/
npx skills add /path/to/groundwork --skill investigation -a codex -g -y  # Codex → ~/.agents/skills/
```

Optional: default model ids in `~/.config/investigation/config.yaml`, so the
agent doesn't have to ask:

```yaml
models:
  nous_session: <model id>
  spotlights_claude: <model id>
  spotlights_codex: <model id>
```

The target (`llm-d-router`) needs a clean tree. Today it has untracked
`.local/` and `docs/metrics-inventory.md`.

## The two runs

Open your agent in `llm-d-router` and ask:

1. **Nous:**
   > Investigate whether the shipped precise-path prefix-cache block size of
   > 16 tokens beats 64 on prefix-cache hit rate, in BLIS. Keep it to one
   > Nous iteration as a Milestone A smoke run.

   Expect it to point out that the configured apparatus root
   (`blis-utilization-aggregation`) has no binary with
   `--block-size-in-tokens`, and to propose the `blis-pd-decider-threshold`
   apparatus instead.

2. **Spotlights static:**
   > Run Spotlights static on pkg/kvcache/kvblock, looking for prefix-cache
   > hit-rate opportunities. One module only.

For each, the agent writes `question.md` and `plan.md` and waits for you to
approve the plan, which grants no launch. Then it prepares the run and
pastes the approval card. Reply with your go quoting the card's short code
(for example "go 3f9a…"). Anything else launches nothing. Ask "status?" any
time. When the run ends, the agent collects, writes the result and stops.

## Keeping the record

Afterwards, copy each study's `runtime/` export and `plan.md` here as
`nous/` and `spotlights-static/`. `tests/test_runtime_acceptance.py` checks
that no results are kept here yet. When they land, update that test and the
plan's R9 status.
