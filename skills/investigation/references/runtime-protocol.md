# Runtime protocol: governed launches (steps 4–5)

Read this before any Nous or Spotlights static launch. Those two methods
launch **only** through `scripts/runtime.py`, never by calling the engine.
The telemetry-driven Spotlights path (`signal-pipeline`) and telemetry
capture (`capture_prom.py`) are **legacy**: they keep their per-launch go
from the owner and are not runtime-enforced (owner decision D10; see
`spotlights.md` and `telemetry.md`).

Paths below:

- `$SKILL`: the folder containing SKILL.md. `RT` = `python3 $SKILL/scripts/runtime.py --study $STUDY`.
- `$STUDY`: `<target>/investigations/<slug>/`.
- `$RUNS`: `$INVESTIGATION_RUNS/<repo>/<slug>/` (doctor prints `runs.root`).

## What the runtime is, and is not

It records requests, prepares an exact invocation, binds the owner's go to
that invocation's digest, launches it once, tracks it, and collects its
output read-only. It is **not a sandbox**. It enforces rules on requests sent
through it. It cannot stop a process that bypasses it, and the engines it
runs have their own powers (Nous runs its own agent session with
`bypassPermissions`). The trust boundary is: the owner trusts the engines at
the pinned versions, and the runtime makes sure only the approved invocation
of them starts.

State lives in `$RUNS/.runtime/` (`runtime.sqlite3`, `prepared/<prep-id>/`,
`logs/`). That DB is the only authority. `$STUDY/runtime/` holds review
copies (`export`); copying them grants nothing.

## The flow

```
submit → prepare → paste card in chat → owner's go quoting the short code
       → approve → launch → status → collect → result
```

1. **Write the request yourself** as an `ains.experiment-request/v1` JSON
   file in the study, from the owner's plain-language question and the
   agreed plan (shape below; examples in `runtime-schemas/examples/`). The
   owner never writes or edits it; see "Where each field comes from". `RT submit <file>`
   validates and records it and prints a receipt. Same id + same payload
   returns the same receipt; same id + different payload is a `conflict`.
   A receipt grants nothing.
2. **`RT prepare <work-id>`** runs the adapter's checks (engine sha == pin,
   semantics, apparatus locks, collisions, clean tree at the request's
   commit) and writes the exact argv and inputs, read-only, under
   `prepared/<prep-id>/`. It prints the **approval card** (at most 6000 bytes,
   also at `prepared/<prep-id>/card.md`). A refusal lists its reasons; fix the
   request (a new revision) and prepare again.
3. **Paste the card verbatim into your own reply.** Tool output is not
   reliably shown to the owner. Then stop and wait.
4. **The owner's go.** Only an explicit go in chat for *that* card, quoting
   its short code, counts. Then, and only then:
   `RT approve <prep-id> --code <short> --source chat --quote "<owner's reply, verbatim>"`.
   The quote must contain the short code. Never infer a go, never carry one
   over from an earlier card or from plan approval.
5. **`RT launch <prep-id>`** revalidates everything (digest, versions, inputs,
   HEAD, clean tree, env), consumes the approval and reserves the invocation
   in one transaction, starts a detached supervisor, and returns at once with
   an `inv-id`.
6. **`RT status <inv-id>`** every 20–30 minutes. It reconciles with native
   state and prints the lifecycle state. Never read engine output to "help"
   it along, and never edit an engine directory.
7. **`RT collect <inv-id>`** once terminal: read-only, retryable, never
   reruns anything.
8. **`RT result <inv-id> [--answer FILE] [--outcome C --explanation T]`**
   builds and exports `ains.experiment-result/v1` to
   `$STUDY/runtime/results/<inv-id>.json`.

Then record the step in `plan.md`'s `## Step log` (below) and stop. The
runtime never chains: a result never starts anything. Any further launch is
a new request, a new preparation, a new card and a new go.

## Requests

Envelope: `schema`, `message_id`, `work_id`, `revision` (a new revision
supersedes the work's request and any unapproved preparation of it),
`target{repo_name, commit}`, `artifacts[]`. Body: `capability`, `question`,
`task_type`, `objective` (text), `baseline?`, `hypothesis?`, `locks{}`,
`limits{}`, `semantics{}`, `next_step?`, `repeat_of?` + `repeat_reason?`.
Missing fields are refused, never inferred. Unknown fields are refused.

| Capability | Required `semantics` | Required elsewhere |
|---|---|---|
| `nous` | `target_system{description, name?}` (all domain context), `models.session`, `objective{metric, direction: increase\|decrease}`, `blis{knobs[], catalog?, limitations[]?}`. Optional: `experiment_commands`, `task_limits{max_runs}`, `locked_workload`, `min_improvement`, `stall_iterations`, `effort`, `session_scope`, `secrets`, `run_id` (8–100 chars, the only non-material key) | `limits.max_iterations`; `locks.blis_binary`, `locks.blis_binary_sha256`, `locks.catalog_commit` (rendered verbatim as `locked_parameters`) |
| `spotlights-static` | `include[]` (slash-form module names that exist at the commit), `models{claude, codex}` (both always passed). Optional: `hints[]`, `repo_url`, `secrets` | non-blank `objective`; `limits.max_cost_usd` is advisory |

### Where each field comes from

| Field | Source |
|---|---|
| `message_id`, `work_id`, `revision`, `semantics.run_id` | you make them up (long, unique; `revision` 1, then +1 per change) |
| `target.repo_name`, `target.commit` | `doctor.py --json` repo block / `git rev-parse HEAD` |
| `question`, `task_type`, `objective`, `baseline`, `hypothesis` | `question.md` and the approved `plan.md` |
| `semantics.target_system.description`, `blis.knobs`, `objective{metric, direction}` | you write them from the plan (`nous-blis.md`) |
| `locks.blis_binary`, `.blis_binary_sha256`, `.catalog_commit` | the apparatus under `blis_apparatus_root` (or the one the plan names): `shasum -a 256`, `git -C <catalog> rev-parse HEAD` |
| `semantics.include` (Spotlights) | module dirs at the commit, from the plan |
| `semantics.models.session` (Nous), `semantics.models{claude, codex}` (Spotlights) | machine config `models: {nous_session, spotlights_claude, spotlights_codex}`; if absent, ask the owner in plain words and offer to save it |
| `limits` | the plan's cost/size section; keep them small unless the owner said otherwise |
| `next_step` | the latest `plan.md` step-log entry |

Every `blis.knobs` entry must be in the representable set (`method-plan.md`).
The adapter also scans the question text for unrepresentable topics; that
scan is a guard, the knob list is the contract.

`next_step{interpretation_of_prior, remaining_uncertainty,
expected_information_gain, rationale}` is copied from the latest `plan.md`
step-log entry. Give it on every request after the study's first result:
`prepare` refuses a request without it once the study has a result, and the
card shows it.
`repeat_of` + `repeat_reason` mark a deliberate rerun of an earlier
invocation; "the result was negative or inconclusive" is never a reason.

## Lifecycle states

| State | Meaning |
|---|---|
| `accepted` | reserved or spawning: launch in progress |
| `running` | engine alive, or exited and not yet classified |
| `cancel_requested` | a cancel is recorded; the engine is **not** confirmed stopped |
| `completed` | engine finished on its own terms. A negative, inconclusive or no-opportunity finding is still `completed` |
| `failed` | engine failed, or its state can't be classified (`unclassified_native_state`) |
| `cancelled` | cancel confirmed from native evidence |
| `recovery_required` | the runtime can't tell whether the engine started. It never relaunches |
| `rejected` | refused, or reconciled as never launched |

## Approval (D1)

The approval records the owner's consent given in chat. It does **not**
prove who typed it. It is single-use and bound to one material digest: any
material change (request, versions, inputs, commit, tree, material env)
makes it unusable, and a new preparation and card are needed. The card shows
what runs, semantics and locks, limits with who enforces each, inferred
defaults, inputs, environment, and how many invocations this study has
recorded so far.

## Limits (D7, D8)

Every limit is labelled `enforced_by: runtime | engine | advisory`.

- **Runtime:** one approval per invocation; no automatic retry or chaining;
  no second equivalent invocation without `repeat_of`; Nous requests must
  carry `max_iterations`.
- **Engine:** Nous `max_iterations`, `stall_iterations`, and its failure
  breaker.
- **Advisory:** money (Spotlights `--max-cost` and any budget are reported,
  never caps) and time (elapsed time is reported against a stated budget;
  nothing is ever killed for time). Nous `task_limits.max_runs` marks an
  iteration partial; it is not a stop.
- No invocation-count caps (D8). The count is on every card.

## Cancellation

Only when the owner asks: `RT cancel <inv-id> --reason "<why>"`.

- **Nous:** `nous stop` (the STOP sentinel). It takes effect only after the
  current iteration's EXECUTE phase, and REPORT then still runs (a full model
  turn). `--interrupt` sends SIGINT instead (exit 130, resumable).
- **Spotlights static:** no native stop. The runtime signals every process
  group of the engine and its descendants (SIGTERM, then SIGKILL). Best
  effort; final reports are not written, and survivors are listed.

A cancel receipt is not a stop. `status` reports `cancel_requested` until
native evidence confirms a terminal state, and if the engine finished first
the real terminal state is reported. Partial artifacts are always kept.
Resuming is a new request with `repeat_of`, a new preparation and a new go.

## Recovery

`status` and `RT recover` reconcile invocations stuck in flight after a
crash. When the runtime can't prove whether the engine started, the
invocation becomes `recovery_required`, with the evidence (live processes
matching the argv, native artifacts). Show the owner that evidence. Only if
the owner confirms in chat that it never launched:
`RT reconcile <inv-id> --mark-not-launched --quote "<owner's reply>"`.
It refuses while a matching process is alive. The approval stays used, so
any new attempt needs a new preparation and a new go.

## Results

`collect` and `result` keep four axes apart:

| Axis | Values | Note |
|---|---|---|
| `execution` | the lifecycle state, native stop reason, exit code | never changed by collection or validation |
| `collection` | `not_run \| collected \| unavailable \| failed` + reason | a failure leaves execution and native output as they were |
| `outcome` | `supported \| not_supported \| inconclusive \| other \| unavailable`, `classified_by: runtime \| coordinator` | the runtime never infers a verdict |
| `validation` | `not_run \| passed \| failed` | `validate_answer.py`, report linked as `report_ref` |

- **Outcome.** Nous comes back `unavailable` (Nous records no study-level
  verdict); Spotlights `candidates_found` / `no_opportunity` come back
  `other`. You set the verdict with `--outcome C --explanation T`, only for a
  completed, collected run, and `supported` / `not_supported` /
  `inconclusive` only when the request stated a hypothesis.
- **Validation.** The answer checked is `--answer`, else
  `$STUDY/final/answer.json`, else the engine's own `answer.json` (Nous).
  `failed` means **fix the answer** and run `result` again. Never rerun the
  experiment because validation failed.
- **Findings** appear only for collected runs: completed, or failed /
  cancelled with native evidence labelled `partial` (a Nous halt). Claims
  carry the answer contract's five keys only. A Spotlights estimate is never
  a claim value; a claim matching one fails the collection.

## After a result: step log, then stop or ask

Write a `## Step log` entry in `plan.md`: interpretation, remaining
uncertainty, the proposed next step **or** the stopping reason, expected
information gain, and the limits that apply. Then one of:

- **Stop.** The answer is good enough, or nothing affordable would change it.
  Stopping needs no launch.
- **Clarify.** Ask the owner; launch nothing.
- **Propose one next step.** For example, a Spotlights candidate becomes a
  Nous request whose `target_system.description` cites the candidate id.
  It is a new request with its own `next_step` block, its own card and its
  own go. Never chain automatically.

## What the adapter runs

This section documents the commands the **runtime's adapters** build and the
supervisor spawns. They are here so the owner can read the card's `argv`.
**Never run them yourself.**

Nous (cwd: the target toplevel; env `NOUS_CAMPAIGN_PARENT=$RUNS`):

```
nous run <prepared>/campaign.yaml --run-id <run_id>
nous stop <work_dir> --reason "ains-runtime cancel <inv-id>"     # on cancel
```

`campaign.yaml` is rendered by the adapter from the request (no `prompts.*`,
`metadata`, `human_gates`). There is no `nous validate` step at prepare: at
the pin it needs a work dir (`nous validate {baseline|design|execute|report}
--dir D`) and has no dry-run mode. A resume (`nous resume`) is not offered;
it would be a new preparation.

Spotlights static:

```
spotlights-engine --repo <target> --include <module> [...] --objective=<text> \
  [--hint=<h> ...] [--repo-url=<url>] --claude-model=<m> --codex-model=<m> \
  [--max-cost=<usd, advisory>] --output-folder $RUNS/spotlights-<prep-id> \
  --artifacts-dir $RUNS/spotlights-<prep-id>/artifacts --no-resume
```

At prepare it also runs the same argv with `--dry-run` (no model or network
call), against a temp dir that is deleted afterwards, to record the models
the engine resolves.
