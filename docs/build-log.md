# Build log — the `investigation` skill and the block-size campaign

> **v2 in progress (2026-11-05).** This log covers v1. v2 turns the fixed pipeline
> into a method-planned investigation (code reading, telemetry snapshots, Spotlights,
> Nous/BLIS, sim2real prep) packaged as an installable Agent Skill. See
> `docs/design-v2.md` for the design and `docs/implementation-plan-v2.md` for the
> phased plan. Paths below (`studies/`, `scripts/`, `skills-src/`, `skill/build.sh`)
> are the v1 layout; studies now live in `examples/` and the scripts in
> `skills/investigation/scripts/`.

Plain-English record of how this repo was built, for Kelly and anyone who picks it up
later. Written while the first real campaign runs. Dates are 2026-11-03 (the build
session) and 2026-11-05 (the campaign session).

## The problem this solves

We keep having one-off questions about llm-d — mostly "does this hand-picked constant
still make sense?" (the rows in the llm-d opportunities list). Answering one properly
takes a chain: look at the real code, run a controlled experiment in the simulator,
and write down the answer with evidence someone else could check. That chain used to
live only in people's heads. This repo packages it as one agent skill, `/investigation`,
so any coding agent can walk the chain the same way every time.

The design was written down and approved first, in two documents:
`~/git/hw2/self-evolving/specs/investigation-skill-spec.md` (the full spec) and
`~/git/hw2/self-evolving/investigation-skill-plan.md` (the short plan). Everything below
follows them, with two owner decisions made along the way.

## The big idea: answers you can check

Every study ends with a small file, `final/answer.json`, that holds a short prose
answer plus a list of claims. Each claim is one number, the exact conditions it was
measured under, and the file it came from. The one iron rule: **the evidence file must
exist and must actually contain that number.** A claim that can't be checked is a bug,
not a claim. We didn't invent this shape — it comes from a research study (the "mlsys"
study, credited in `skills/investigation/references/answer-contract.md`) — and it turns out Nous v2 independently
adopted the same shape for its own answers, so everything speaks one language.

## Day 1 (Nov 3): the repo, the tools, the skill — no spend

Built with five parallel subagents, one commit per step:

1. **Scaffold** (`5662dc3`) — this repo: README with credits, the studies/runs split
   (studies are small and committed; runs are gigabytes of simulator output and are
   gitignored), and the hash-lock script template copied from the A1 apparatus.
2. **Docs** (`b70782a`) — the answer contract (the mlsys text, word for word,
   credited), the ten-item hand-scoring checklist the owner fills in after every
   study, and one page mapping each artifact to its future Hive counterpart.
3. **The skill** (`7be8b5f`) — `/investigation`, built from `skills-src/` by a small
   `build.sh` and installed to `~/.claude/skills/investigation/`. It's a ten-step run
   book: take the question in, check the simulator can represent it, ground it in the
   real code (Spotlights or a plain code search — decided honestly, either way written
   down), author the campaign, **stop and wait for the owner's "go"**, launch, monitor,
   collect, answer, validate, hand-score. Three hard gates at the top: never launch
   without a go; never touch engine state; never write into a study's `final/` without
   the owner seeing the draft first.
4. **The validator** (`a3ebf81`) — `scripts/validate_answer.py`, about 300 lines of
   stdlib Python. It re-opens every evidence file and mechanically confirms the claimed
   number is really there (numbers compared with the same tolerances Nous v2 uses;
   big files streamed, never loaded whole). 29 tests, including the key negative: a
   file that exists but doesn't contain the value.
5. **The collector** (`f335bfd`) — `scripts/collect.py`, reads a finished Nous v2 work
   directory read-only and writes one `collection.json` into the study: what ran, what
   the guards said, and a draft claims list taken from the campaign's own answer. A
   test proves it never modifies the work dir (file timestamps identical before and
   after). It refuses old-format (pre-v2) work dirs on purpose.
6. **The retrospective study** (`3dbf597`) — the reference example. We re-answered the
   already-finished A1 question ("does a fitted P/D threshold rule beat the shipped
   T=16?") through the new contract, using A1's real files as evidence. Answer: no,
   keep 16 — the rule wins big where it engages (+29.7% to +83.4%) but breaks its
   guardrails on held-out layouts, so the gated verdict is −100.0%. All five claims
   validated against the real files. This exercise caught a genuine error in the
   spec's own worked example: it cited `best_found.json` as the source of the −100.0
   verdict, but that file doesn't contain it — the number lives in the analysis's
   `objective_metadata.json`. The validator caught it; the study records the
   correction.
7. **The block-size study setup** (`5183886`) — question, falsifier, and grounding for
   the pilot: *llm-d chunks the prefix cache into fixed 16-token blocks — does 16
   still hold, and where does it break?* Pre-registered rule: 16 stands unless some
   other size in {8, 32, 64} gains 5% or more on any workload while staying inside the
   latency gates. Grounding was a deliberate code search (not Spotlights — it's a
   numeric sweep over a documented knob): the lever lives in
   `llm-d-router/pkg/kvcache/kvblock/token_processor.go`, default 16 on the precise
   path; the approximate path already auto-tunes with a 64 floor; and no sensitivity
   sweep exists in llm-d-benchmark — that gap is this study.

**The mid-build surprise: Nous v2.** The spec assumed Nous v0.5.0. Kelly corrected this
early: build against Nous v2, which lives on its own branch (`origin/nous-v2-fixes`).
Exploration showed v2 is a complete rewrite: it runs one Claude session instead of a
six-phase state machine, it deleted the old result files our collector was going to
read, it fixed the "silent zero" scoring bug by deleting the scoring it lived in, and —
the good news — its native answer format is exactly our claims contract, and it
validates evidence itself. So the collector was written fresh against v2's file
shapes, the skill cites v2's commands (the old `--auto-approve`/`--timeout` flags are
errors now), and the v2 CLI was installed on this machine from the branch (via a local
branch ref, so the strategy-evolution repo's working copy was never touched). Side
effect worth knowing: the old installed nous-* skills that pass removed flags will
error until refreshed.

## Day 2 (Nov 5): the campaign — steps 7 and 8

Kelly said go ahead with the driver scripts and the launch.

**One decision, asked and answered:** on the Weka trace, A1 had run block size 64 (to
match that trace's 64-token hash blocks), so "the shipped default" was ambiguous
there. Kelly chose **B-ship = 16 uniformly** (`a109eaf`) — it answers the actual
question, and the 64 arm on Weka doubles as a test of whether A1's old choice
mattered.

**The experiment scripts** (at the apparatus, `/Users/kelly/git/nous-run/nous-campaigns/
blis-pd-decider-threshold/scripts/blocksize/`, hash-locked with their own
`MANIFEST.sha256`, sha `529463ef…`): adapted from the proven A1 scripts by a
subagent —

- `blocksize.py` — the definitions: four arms (block size 8/16/32/64), everything else
  held at shipped config (the P/D decider stays at its shipped threshold-16 rule so
  only block size moves), every run passes the block size explicitly so nothing is
  inherited, and A1's load-ladder anchors are reused (block 16 is flag-identical to
  A1's control arm).
- `driver.py` — the scheduler: runs the sweep cell by cell, idempotent (a restart
  reuses finished runs), refuses to start if the simulator binary's hash changed.
- `analysis.py` — the locked, pre-registered analysis: computes capacity per
  workload/layout/arm, and the one verdict number — `blocksize_max_gated_gain_pct`,
  the biggest gain any alternative manages over 16 anywhere, counted only when it
  holds inside the latency gates. **16 stands iff that number is under 5.0.** It also
  reports the per-workload curve (the actual deliverable), cache hit rates, and
  integrity checks (a determinism sample and a zero-reuse control).
- Before trusting them, the subagent tested the pieces individually — including a full
  dry-run of the analysis on a fabricated results directory, which caught one real
  bug (a wrong argument type) before it could touch the campaign.

**The campaign file** (`bf28ca5`) — `studies/blocksize-sweep/campaign.yaml`, written
for Nous v2: the whole brief lives in `target_system.description` (that's where v2
reads domain context), the objective is the verdict metric, and the plan is fixed:
BASELINE runs the cheap seed-42 rehearsal; iteration 1 re-runs it and must match the
baseline analysis exactly (a tamper check); iteration 2 runs the six pre-launch checks
and then the full sweep; the session may cite only files inside its own work dir, so
every number it reports is checkable.

**The launch, in three acts:**
1. All pre-launch checks passed (manifest verifies, catalog pinned, 714 GB free,
   nothing running) — but the first launch died in seconds: the API endpoint refused
   the model `claude-sonnet-5` (403 — the catalog now serves GLM models; the model
   names A1 used in October no longer work on this path). No spend, no results lost.
2. With Kelly's confirmation, the session model switched to `rits/zai-org/glm-5-3`
   (`b40acc0`) — the same model her Claude Code runs on, through the identical
   authentication path.
3. Relaunched clean: the campaign is running now. It verified the apparatus itself
   (as its brief tells it to), and is in BASELINE — the rehearsal sweep. Expected
   shape: under an hour of rehearsal, then the full sweep — roughly 4,000–5,000
   simulator runs, about half of A1's campaign, so ~4–6 hours — then the locked
   analysis and the answer.

## What's watched, and what's next

The campaign runs unattended with two tripwires: the completion notification, and a
background watcher that fires if the work directory goes quiet for 45 minutes. The
skill's rule stands — anything that looks wrong gets reported, never fixed in flight.

When it finishes: collect (`scripts/collect.py`), write the study's `final/answer.json`
from the real artifacts, validate it (must exit 0), and hand the scoring checklist to
Kelly. Two things to keep in mind when reading results, both pre-registered so they're
findings rather than surprises: block 16 on Weka is a first for this apparatus (the
ladder may run longer there), and any "16 fails" verdict should be read together with
the `guardrail_broken_wins` list before anyone acts on it.

Known limits that will appear in the answer: BLIS cannot represent the approximate-path
LRU or `maxPrefixTokensToMatch`, so this sweep speaks only about the precise block-size
axis; and block size feeds the P/D decider's arithmetic, so with the decider threshold
held fixed, changing block size can move disaggregation decisions too — measured, not
hidden.

Still open after this campaign: refreshing the old installed nous-* skills for v2,
updating the hw2 spec's v0.5.0-isms, and a line in hw2's progress.md — each needs
Kelly's go since they live outside this repo.
