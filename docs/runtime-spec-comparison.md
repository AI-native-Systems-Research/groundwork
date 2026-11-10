# AINS runtime spec vs groundwork: comparison

Date: 2026-11-09. Compares [ains-runtime-spec.md](ains-runtime-spec.md) (the
proposed AINS Local Capability Runtime, v0) with the current groundwork design
([design-v2.md](design-v2.md)) and its implementation
([SKILL.md](../skills/investigation/SKILL.md), the files under
[references/](../skills/investigation/references/) and
[scripts/](../skills/investigation/scripts/), as of commit `46111ff`).

## Summary

The two mostly agree on intent: the agent reasons, the owner approves every
expensive launch, and negative results count. They put the effort in
different places:

- **The runtime spec is a control layer:** typed messages, a deterministic
  runtime, approvals tied to the exact invocation, duplicate and loop
  protection.
- **groundwork is an evidence layer:** method planning, the answer contract, a
  validator that checks each claim, telemetry and code citations.

Each is thin where the other is strong, so they fit together more than they
compete. There are three real conflicts (see the last section).

## Where they agree

| Topic | Runtime spec | groundwork today |
|---|---|---|
| Entry point | "Install the skill, not the system" (§2.1) | `npx skills add … --skill investigation` |
| Expensive steps need approval | One approval per Nous/Spotlights invocation (§11) | Hard gate 1: one "go" per launch; approving the plan isn't a go |
| No automatic chaining | A result never triggers the next capability (§2.3, §21) | Each launch is gated; Spotlights → Nous goes through a file the agent writes, citing the candidate id |
| Don't make up inputs | Reject missing fields rather than invent them (§15) | BLIS is "never quietly approximated"; never fakes `traces.jsonl` or `01_signals.json` |
| Negative results count | `unsupported` / `inconclusive` are valid results (§16) | "Report negative results as results"; a pushback can be the whole result |
| Native artifacts are authoritative | Keep all native output (§23.2, §29) | Engine state is read-only (gate 2); runs dir sits outside the repo |
| Version pinning | Record versions on every invocation (§19) | `nous-pin` / `spotlights-pin` in the skill's frontmatter; `doctor.py` warns on drift; `question.md` records the versions used |
| First run | Detect, check it runs, stop with setup instructions; no auto-install (§20) | `doctor.py` reports which methods are available and why not; never installs anything |
| Local state | Local, scoped to the project (§18) | `investigations/<slug>/` plus the runs dir |
| Nous campaign fields | `research_question`, `task_type`, `target_system.{name, description}`, `objective`, `locked_parameters`, `max_iterations` (§13–14) | Same fields in [nous-blis.md](../skills/investigation/references/nous-blis.md) |
| Non-goals | No PRs, no fixed Spotlights → Nous pipeline, no autonomous loops | Same; signal-pipeline stage 05 and sim2real deploy are banned |

## Where they differ

### 1. Who enforces the rules (the biggest gap)

- **Runtime spec:** a deterministic runtime enforces approvals,
  preconditions, budgets, retries, duplicate prevention and loop limits. The
  LLM proposes; the runtime decides.
- **groundwork:** the gates are prose in SKILL.md, and the only enforcer is the
  agent following them. Only a few things are checked by code:
  - `validate_answer.py`;
  - `new_study.py --commit-guard`;
  - Nous's own `locked_parameters` check;
  - the pre-launch shell checks (`pgrep` for a running job, `ls` for a run_id
    collision). These are a rough, unrecorded form of duplicate prevention.
- Nothing records approvals and nothing tracks budget. A "go" exists only in
  the chat.

### 2. Planning model (a direct conflict)

- **Runtime spec §2.3:** "must **not choose a complete workflow up front**."
  Plan one step ahead, then re-plan from the result.
- **groundwork:** step 2 writes `plan.md` with "Order and hand-offs" (e.g.
  telemetry → Spotlights → Nous), which the owner approves up front. There is
  no explicit re-plan step after a method returns. The flow just goes on to
  Collect.
- The two can coexist if `plan.md` is a non-binding sketch and each step
  becomes its own request. As written, though, they contradict each other.

### 3. What a study produces

- **groundwork:** `answer.json` under the
  [answer contract](../skills/investigation/references/answer-contract.md).
  Every claim's evidence file must exist and contain the value, and
  `validate_answer.py` checks that mechanically. Code citations are checked
  against the git sha. Then the owner hand-scores it.
- **Runtime spec:** `experiment.result.v1` with `outcome.classification` and
  loose `findings`. It has no claim-level evidence contract and no validator.
  - groundwork's evidence checking is its core asset. The spec would lose it
    unless `findings` carries contract claims.
  - Going the other way, groundwork has no explicit supported / unsupported /
    inconclusive field, and that would be worth adding.

### 4. Messages and lifecycle

- **The runtime spec has:**
  - a message envelope (`message_id`, `work_id`, `revision`, `reply_to`);
  - receipt vs result;
  - progress, clarification and cancel messages;
  - work states that are kept separate from the experiment outcome (§7, §17).
- **groundwork has none of these.** Hand-offs are files (`campaign.yaml`
  description, `collection.json` keys). Lifecycle lives in Nous's own
  `state.json` and `nous status`. "Runtime failure vs domain outcome" is
  handled by prose ("report it, never fix it"), not a defined status.

### 5. Approval tied to the exact invocation

- **Runtime spec §11:** approval is bound to a fingerprint of the fully
  resolved invocation. Any material change (question, commit, objective,
  locks, version) needs a new approval.
- **groundwork:** the owner says "go" to a 2–3 sentence summary. Nothing is
  fingerprinted, so a change after approval isn't detected. Locking the BLIS
  binary sha and catalog commit covers part of this, but only for the tool.

### 6. Adapter structure

- **Runtime spec:** each capability gets `validate / prepare / execute /
  collect`. `prepare` produces a recorded artifact: the command, env, working
  directory and expected outputs.
- **groundwork:** the same four parts exist, but scattered:

  | Spec stage | groundwork equivalent | How it's done |
  |---|---|---|
  | validate | `nous validate` plus the pre-launch checks | the agent runs CLI and shell checks |
  | prepare / execute | the steps in `nous-blis.md` and `spotlights.md` | the agent reads prose and runs the commands |
  | collect | `collect.py`, `collect_spotlights.py`, `collect_telemetry.py` | Python scripts |

  Nothing records the prepared invocation.

### 7. Scope

- **Runtime spec POC:** Nous only, general rather than BLIS-specific, with
  Spotlights deferred (§24).
- **groundwork:** already covers six methods: code reading, telemetry
  snapshots, Spotlights static, Spotlights telemetry, Nous on BLIS, and
  sim2real prep. Its Nous path is tied to a BLIS apparatus and a table of what
  BLIS can represent.
- Not in the runtime spec at all:
  - telemetry capture;
  - code citations;
  - sim2real;
  - hand-scoring;
  - the fork/untracked tracking modes;
  - pushback as a first-class outcome (the spec only has "stop").

### 8. Intake

- **Runtime spec §12:** "Infer aggressively, ask selectively." Required inputs
  are minimal.
- **groundwork:** intake must record more before anything runs: a falsifiable
  form ("wrong if…" with metric, threshold and comparator), a named baseline,
  qualitative vs quantitative, and a check for prior studies or Nous
  principles. That is stricter, and it is what makes the answers checkable.

### 9. Budgets and retries

- **Runtime spec:** budgets are enforced, retries need a policy, invocation
  counts are bounded.
- **groundwork:** a rough cost table in `plan.md`, plus engine-level caps:
  Spotlights `--max-cost`, Nous `max_iterations` and `task_limits.max_runs`.
  Retries are effectively banned ("never re-run a simulator job to fix a
  result"; `nous resume` counts as a launch and needs its own go). Nothing
  keeps a tally.

## Open gaps in the runtime spec that groundwork already answers

Most of what §23 lists as unresolved is already worked out in the skill's
references:

- **§23.1 Nous invocation** (see
  [nous-blis.md](../skills/investigation/references/nous-blis.md)):
  - launch: `NOUS_CAMPAIGN_PARENT=… nous run campaign.yaml --run-id <id>` (no
    `--auto-approve`, no `--timeout`);
  - also `nous status`, `nous stop` (clean halt at a phase boundary),
    `nous resume`, `nous validate`, `nous schema`;
  - Nous works in a git worktree under `.nous-experiments/`.
- **§23.2 Nous artifacts:** `state.json`, `ledger.json`, `baseline.json`,
  `plan.json`, `findings.json`, `answer.json`, `final_check.json`,
  `principles.json`. Phases: BASELINE → DESIGN → EXECUTE → REPORT, with
  predictions locked at the end of DESIGN. `collect.py` already parses these.
- **§14 mapping:** the spec's mapping leaves out a few fields:
  - `models.session`, `run_id`, `experiment_commands`,
    `task_limits.max_runs`, `min_improvement`;
  - the trap that domain context only reaches the agent through
    `target_system.description` (anything put in `prompts.*` or `metadata` is
    silently ignored).
- **§23.3 and §23.4 Spotlights** (see
  [spotlights.md](../skills/investigation/references/spotlights.md)):
  - the full `spotlights-engine` and `signal-pipeline` command lines;
  - the `SpotlightReport` schema: candidates have stable ids
    `cand-<module>-NNNN`, file and line locations, `estimated_impact`, and
    proposals;
  - `run_manifest.json` records the target commit and cost;
  - stage 05 changes the subject repo;
  - telemetry from metrics alone isn't accepted yet.
  - Also the rule that a Spotlights estimate is a hypothesis and never a claim
    value. That answers the spec's question about "opportunity discovery vs
    recommendation".

## Conflicts that need a decision

1. **Hive/Crux direction.** The runtime spec's whole portability argument
   (§1, §3) assumes these contracts will later run inside Hive/Crux.
   design-v2 says the opposite ("It assumes Hive's lab will absorb this repo.
   That's no longer happening."), and `docs/hive-mapping.md` was deleted. One
   of the two needs updating.
2. **Up-front plan vs one-step-ahead.** Keep `plan.md` as an approved
   sequence, or demote it to a non-binding sketch with an explicit re-plan
   after each result?
3. **Where enforcement lives.** Add a small deterministic runtime under the
   skill, or keep the rules in prose? If one is added, a stdlib Python script
   in `scripts/` would match the existing style. It would record each approval
   with a fingerprint and refuse repeated `message_id`s and unchanged re-runs.
   It would also wrap `nous run` and `spotlights-engine` and keep the
   `prepare` record.

## Recommendation

Treat the runtime spec as the control layer *underneath* the existing skill,
not a replacement:

- Keep groundwork's answer contract and validator as what the result has to
  satisfy, and carry them in `experiment.result.v1.findings`.
- Fill in the spec's §23 from the existing references rather than researching
  it again.
- Start with the Nous path, as the spec's §24 says, since `collect.py` already
  handles Nous output.
