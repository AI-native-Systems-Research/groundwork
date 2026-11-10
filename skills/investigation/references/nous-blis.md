# nous-blis: a Nous v2 campaign in the BLIS simulator (step 4)

Read this only when the plan includes `nous-blis`. Hard gates 1 and 2
apply: the campaign launches only through `scripts/runtime.py`, on the
owner's explicit go for that approval card. The flow, the CLI and the result
record are in `runtime-protocol.md`; this file covers what goes into the
request.

Paths below:

- `$SKILL`: the folder containing SKILL.md.
- `ROOT`: the BLIS apparatus root, `blis_apparatus_root` in the machine config
  (doctor prints it). It holds the pinned BLIS binary, the catalog (a git repo
  pinned at a commit), the traces and `env.sh`. Referenced by absolute path,
  **never copied and never modified**.
- `$STUDY`: `<target>/investigations/<slug>/`.
- `$RUNS`: `$INVESTIGATION_RUNS/<repo>/<slug>/` (doctor prints `runs.root`).

## Read the v2 docs first

In the Nous checkout (`engines.nous` in the machine config; doctor shows its
path and sha), read `docs/campaign-authoring-guide.md` and `docs/protocol.md`
at the pinned ref, read-only, without checking anything out:

```bash
git -C <nous checkout> show nous-v2-fixes:docs/campaign-authoring-guide.md
git -C <nous checkout> show nous-v2-fixes:docs/protocol.md
nous schema <artifact>          # JSON Schema for any v2 artifact
```

## Campaign authoring facts (Nous v2)

You no longer write `campaign.yaml` by hand. The runtime's Nous adapter
renders it from the `experiment-request` (`runtime-protocol.md`, "Requests"),
read-only under the preparation. The facts below say what each request field
becomes, so the request is right the first time:

- `body.question` → `research_question`; `body.task_type` → `task_type`;
  `semantics.objective{metric, direction}` → `objective`;
  `limits.max_iterations` → `max_iterations`; `semantics.models.session` →
  `models.session`; `body.locks` → `locked_parameters`, verbatim;
  `semantics.target_system.description` → `target_system.description`, with
  the request's objective, baseline and hypothesis appended. The adapter sets
  `repo_path` and `run_id`.

The native fields:

- Required: `research_question` and `target_system.repo_path`. The adapter
  sets `repo_path` to the target repo's toplevel. Nous works in a git worktree of
  it (`<repo>/.nous-experiments/<run_id>`, branch `nous/<run_id>`), never in
  the checkout itself.
- **All domain context goes in `target_system.description`.** v2 copies
  campaign.yaml into the work dir and the agent reads it directly. Put the
  apparatus rules (binary path, what not to modify), the arms, the metric
  definitions, the verdict rule, the evidence-citation rules and the known
  limits there.
  When the plan chains methods, the description cites the Spotlights candidate
  id and the telemetry capture id it builds on.
- `objective` is `{metric, direction}`. There are no weights and no
  `deploy_threshold`.
- `task_type` is one of `debug | verify | optimize | policy | explain | other`.
  A pre-registered check of a shipped default is `verify`.
- `locked_parameters` is a flat map, enforced against every planned run. Runs
  in `plan.json` must spell locked keys exactly as they appear here, with values
  copied verbatim (or list the key in `workload_changes`). The swept variable is
  **not** locked. Lock the BLIS binary path and its sha256, and the catalog
  commit (`locks.blis_binary`, `locks.blis_binary_sha256`,
  `locks.catalog_commit`; all three are required). Lock workload facts the question fixes (model, pod count, durations)
  so the agent can't drift them.
- `models.session` names the session model. `max_iterations` caps iterations.
  `run_id` names the work dir.
- Optional and useful: `experiment_commands` (regexes for what counts as an
  experiment run; don't match polling scripts), `task_limits.max_runs`,
  `min_improvement`, `effort`.
- **No** `prompts.*`, `ground_truth`, `iterations`, or `metadata` keys. v2
  ignores them, and context put there silently never reaches the agent.
- There is no campaign-level `nous validate` and no dry-run at the pin:
  `nous validate {baseline|design|execute|report} --dir D` checks a phase's
  artifacts in an existing work dir. So read the description through before
  submitting, and let `prepare` check the rest.

A request in this shape is `runtime-schemas/examples/experiment-request.json`.
The older worked example, `examples/blocksize-sweep/campaign.yaml` in the
skill's source repo (groundwork), is in the v1 layout with machine paths:
read it for the description's content, never run it.

## Who does what

Nous v2 designs and runs the experiment itself: BASELINE measures the system
(this is where calibration happens), DESIGN writes the hypotheses and the run
plan (`plan.json`), EXECUTE runs it, REPORT writes `answer.json`. Predictions
lock at the end of DESIGN. **Don't build the experiment for it.** No driver,
no run matrix, no per-arm configs, workloads, or calibration runs, written by
you before launch. That duplicates Nous and fights its plan validator.

Your job is three things:

1. **Make sure the tool exists.** If an arm needs behaviour the stock BLIS
   binary doesn't have (a patched router, a new flag), build it before
   launch and add it to `references/upstreams/lock.json` (that's a change to
   the skill, so it's the owner's call; `references/upstreams/README.md`).
   Nous measures with the tool; it never modifies it mid-campaign.
2. **Write the request** (above), with the question, the arms, the seeds
   or seed count, the verdict rule, and **exact metric definitions** in
   `semantics.target_system.description`, and the BLIS flags it varies in
   `semantics.blis.knobs`. Be precise enough that two people would
   compute the same number from the same output (what counts as "the hot
   pod", the measurement window, warm-up excluded, thresholds). Do the
   closed-form sanity check from the authoring guide before locking values.
3. **Prepare, get the go, launch, monitor, collect and write the answer**
   (below).

Analysis code: by default Nous writes its own, and it must do so during
DESIGN, before any treatment run, since the definitions are already fixed in
the description. If the verdict turns on a computation subtle enough that
post-hoc choices could move it, the owner may ask you to write one analysis
script up front and lock its sha256 too. Ask; don't do it by default.

## Pre-launch checks (prepare runs them)

`runtime.py prepare <work-id>` refuses, with reasons, unless all of these
hold. It runs them again at `launch`.

- Nous is available and its sha equals the locked commit (`references/upstreams/lock.json`).
- The request carries the required semantics and `limits.max_iterations`,
  and every `blis.knobs` flag is representable.
- `sha256` of the locked BLIS binary equals `locks.blis_binary_sha256`, and
  the catalog's HEAD equals `locks.catalog_commit`.
- That sha256 is one of the BLIS binaries in `references/upstreams/lock.json`,
  and that commit is its catalog commit. A new or patched build goes into the
  lock first (sha256, source commit, router, patches).
- No work dir, `.nous-experiments/<run_id>` worktree or `nous/<run_id>`
  branch exists for the run id, and no `nous run|resume` or BLIS process is
  already running (one campaign at a time).
- The target is at the request's commit with a clean tree (`investigations/`
  and `.nous-experiments/` are ignored).

The card shows free disk under the runs dir. Full sweeps need ~100 GB.

## Approval and launch

Paste the card verbatim into your reply. It says what runs, the iterations,
the locks, the limits and who enforces each, and the cost note (Nous reports
cost incompletely; it is advisory). Add a sentence on the rough wall-clock
time and disk. **Stop and wait** for the owner's go quoting the short code,
then `approve` and `launch` (`runtime-protocol.md`).

The work dir lands at `$RUNS/<run_id>/`, outside the repo. Nous's git
worktree still goes under the target repo's `.nous-experiments/`. If API
environment variables are needed, they come from the owner's agent settings
and are never echoed.

## Monitor

- `runtime.py status <inv-id>` every 20–30 minutes. Read-only, you may also
  look at Nous's phase artifacts (`baseline.json`, `plan.json`,
  `findings.json`) in the work dir. `metrics.json` files hold per-request
  arrays, so never cat them.
- If something looks wrong (binary changed, runs outside the plan, a stalled
  phase), report it. Never fix it (hard gate 3).
- Stopping needs the owner's word: `runtime.py cancel <inv-id>`. It writes
  Nous's STOP sentinel, which takes effect only **after the current
  iteration's EXECUTE phase**; REPORT then still runs, a full model turn.
  Until that is confirmed, status says `cancel_requested`. `--interrupt`
  sends SIGINT instead (exit 130, resumable).
- Resuming is a new request (`repeat_of` + `repeat_reason`), a new card and
  a new go.

## Collect

`runtime.py collect <inv-id>`, then `runtime.py result <inv-id>`. Collection
is read-only over the work dir and reuses `scripts/collect.py`, which merges
its section into `$STUDY/collection.json` under the key `nous` (other
methods' keys are kept). It reads `state.json`, `ledger.json`, `answer.json`,
`final_check.json` and `baseline.json`, and drafts claims with verified
flags. Copy the cited evidence files into the study so the claims resolve
there. A Nous result's outcome is `unavailable` until you give
`--outcome … --explanation …` against the request's hypothesis. Then write
the step-log entry in `plan.md` and go to step 6 (`answer-contract.md`).

## Known limits to carry into every BLIS answer

- What BLIS can't represent (see the table in `method-plan.md`) is outside the
  claim. Say so in the answer text.
- Any confound the held parameters create (e.g. block size feeds the P/D
  decider arithmetic while the threshold is held) gets stated, not "fixed".
- Workload caveats (working sets that fit in cache, sample-dependent trace
  capacity) carry over, including any Nous recorded in `baseline.json`.
