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
  Requires git, Python 3.10+ and PyYAML (doctor.py and validate_answer.py are
  stdlib-only). Optional per method: kubectl or network access to Prometheus;
  spotlights-engine and signal-pipeline; Nous v2 and a BLIS apparatus; a
  sim2real checkout.
metadata:
  version: "2.0.0"
  runtime-version: "0.1.0"
---

# `investigation`

Answer a one-off question about the repo this session is in, with evidence
someone else can re-check.

## When to use

- The owner asks a one-off question about this repo ("does the 16-token block
  size still hold?", "where does the EPP spend its time under our traffic?").
- The owner asks to run an investigation, or to answer an opportunities row.
- The owner wants to check whether a shipped constant or default still holds.

## Hard gates

These override everything else in this file.

1. **Never launch without a go.** No Nous run/resume/reconfigure, Spotlights
   or signal-pipeline run, or telemetry capture unless the owner said "go" for
   *that* launch in this conversation. Approving the plan is not a go for the
   launches in it. One go per launch.
2. **Governed launches go through the runtime only.** Nous and Spotlights
   static launch only via `scripts/runtime.py` (submit → prepare → approve →
   launch). Never call `nous run`/`nous resume` or `spotlights-engine`
   yourself. Run `approve` or `reconcile` only right after the owner's
   explicit go in chat for *that* card, quoting the owner's reply and the
   card's short code. Never infer a go or carry one over.
3. **Read-only over engine state.** Never edit a running or finished Nous work
   dir or Spotlights run dir. Never re-run a simulator job to "fix" a result.
   Never tune anything after seeing results.
4. **Show before writing.** Nothing is written into a study's `final/` until
   the owner has seen the draft.

Close to gates: never change the question mid-study without the owner, and
report negative results as results.

**The owner speaks plain language; you write every structured input.** Never
ask the owner for JSON, YAML, a file path to a request, a sha256, a commit, a
module name or a run id. Derive them: the commit and repo from git, engine and
apparatus facts from `doctor.py --json`, binary sha256 and catalog commit by
hashing and reading the apparatus, module names from the tree, ids you make
up. Model ids come from the machine config's `models` key; if one is
missing, ask in plain words ("which model should Nous's session use?") and
offer to save the answer there. Ask the owner only for real decisions (the
question, the arms, a go), never for fields. The approval card is how the
owner reviews what you wrote.

## Where things live

- **Skill scripts and references:** resolve every `scripts/…`, `references/…`
  and `assets/…` path in this file against **the folder that contains this
  SKILL.md** (it may be installed under `.claude/skills/`, `.agents/skills/`,
  or a global dir), never against the target repo. Below, `$SKILL` means that
  folder.
- **Target repo:** `git rev-parse --show-toplevel` of the session.
- **Study:** `<target>/investigations/<slug>/` (small, portable text).
- **Runs dir:** `$INVESTIGATION_RUNS/<repo>/<slug>/`, default
  `~/.cache/investigation/<repo>/<slug>/`, outside the repo and never
  committed. Heavy engine output goes there.
- **Config:** per repo `investigations/config.yaml` (or
  `~/.config/investigation/repos/<repo>.yaml` in untracked mode); per machine
  `~/.config/investigation/config.yaml` (engine checkouts, BLIS apparatus
  root, runs dir, default `models` ids). Template: `assets/config.yaml.tmpl`.
- **Committing:** before any commit that touches `investigations/`, run
  `python3 $SKILL/scripts/new_study.py --commit-guard`. It refuses when the
  branch's push remote is upstream. Never add `investigations/` to an upstream PR.

## The steps (0–9)

### 0. Preflight

Run `python3 $SKILL/scripts/doctor.py --json` (add `--offline` without
network). Read `methods`: the plan may only pick methods with
`available: true`, and it lists the others with doctor's reason. Relay any
pin/version warnings to the owner. Detail: `references/method-plan.md`.

### 1. Intake

Scaffold with `python3 $SKILL/scripts/new_study.py --slug <slug> --question "…"`
(first study in a repo: add `--tracking fork|untracked` after asking the
owner). Fill `question.md`: the question, where it came from, the falsifiable
form ("this is wrong if…"), the baseline (usually the shipped default,
"B-ship"), and whether it is **qualitative** or **quantitative**. First grep
earlier `investigations/` and Nous `principles.json` files under the runs dir
for prior learnings. Detail: `references/method-plan.md`.

### 2. Method plan — GATE

Write `plan.md`, a **provisional method outline**: methods chosen, why each
and why not the others, a likely order, cost and time, claim kinds, pushback.
It is revised after every result in its `## Step log`. Pushing back is
allowed and can end the study here: unfalsifiable as posed, data we can't
get, already answered, or a pure lookup. BLIS-unrepresentable questions
exclude `nous-blis`; they never get approximated. **Show the plan and wait
for approval.** Approving it grants no launch. Detail:
`references/method-plan.md`.

### 3. Telemetry (optional; its own go)

**Legacy gate, not runtime-enforced** (until the runtime governs captures).
Recommend a window in absolute RFC 3339 times, get a go, then capture a
frozen snapshot with `scripts/capture_prom.py` into `telemetry/<id>/`. A live
endpoint is never evidence. Tokens come from an env var only and are never
echoed. Detail: `references/telemetry.md`.

### 4. Run methods — GATE per launch

**Nous and Spotlights static** go through the runtime, one step at a time:
write an `experiment-request` JSON, `runtime.py submit`, `runtime.py prepare`,
**paste the approval card verbatim into your reply** and wait. On the owner's
go quoting the short code, `runtime.py approve … --source chat --quote "…"`,
then `launch`, then `status`. One request, one card, one go per launch. For
Nous, you describe the experiment and lock the tool (BLIS binary path and
sha256, catalog commit); the adapter renders `campaign.yaml`, and Nous
designs, calibrates and runs the experiments itself. Don't pre-build configs,
workloads or a driver. Detail: `references/runtime-protocol.md`,
`references/nous-blis.md`, `references/spotlights.md`.

**Spotlights telemetry-driven** (`signal-pipeline`) is a **legacy gate, not
runtime-enforced**: a 2–3 sentence summary of what runs and what it costs,
then its own go. It runs to stage 04 at most, never stage 05. Spotlights
output goes to the runs dir; the study keeps only what it cites.

### 5. Collect, record, decide

Runtime launches: `runtime.py collect <inv-id>`, then `runtime.py result
<inv-id>` (add `--outcome … --explanation …` for your verdict; the runtime
never infers one). Legacy paths: `scripts/collect_spotlights.py`
(`spotlights`), `scripts/collect_telemetry.py` (`telemetry`). All are
read-only and merge their own key into `collection.json`. After each result,
add a `## Step log` entry to `plan.md` (interpretation, remaining
uncertainty, next step or stopping reason, expected information gain,
limits). Then stop, ask the owner, or propose one next step as a new request
with its own card and go. Nothing chains automatically, and stopping needs
no launch. Start the answer from the drafted claims, keeping only each claim's five contract keys
(`id, metric, value, condition, evidence`) and dropping draft-only fields such
as `verified` and `candidate_id`. Detail: `references/answer-contract.md`.

### 6. Answer — GATE

Draft `final/answer.json` with exactly `{answer, deliverable, repro, claims}`.
Every number in the prose is a claim. Code citations are allowed only for
qualitative questions. A Spotlights estimate is never a claim value. Show the
draft to the owner before writing it. Detail: `references/answer-contract.md`
(see "Evidence kinds (v2)").

### 7. Validate

`python3 $SKILL/scripts/validate_answer.py <study>/final/answer.json`, plus
`--check-citations [--repo NAME=PATH]` when there are code citations. It must
exit 0. Fix the claims, never the validator, and never rerun the experiment
because validation failed. For runtime launches, run `runtime.py result
<inv-id>` again so the result record carries the passing validation. Detail:
`references/answer-contract.md`.

### 8. sim2real prep (optional, Nous only)

After a Nous win, if the owner wants a path to a real validation, prepare
(never deploy) a bundle under `sim2real/`: algorithm bootstrap or `--byo`
knob overlay, plus the printed register command. Detail: `references/sim2real.md`.

### 9. Hand-score

`new_study.py` already put `hand-score.md` in the study. Walk the owner
through the items (including "was the method plan right in hindsight?") and
record their scores. Detail: `references/hand-scoring.md`.

## What this skill will never do

- Launch, resume or reconfigure Nous, Spotlights, signal-pipeline, or a
  telemetry capture without an explicit go for that launch.
- Launch Nous or Spotlights static around the runtime, or chain one method's
  result into another launch without a new request and a new go.
- Run signal-pipeline stage 05, or deploy anything (sim2real deploy, Tekton).
- Edit anything inside a Nous work dir, a Spotlights run dir, or the BLIS
  apparatus.
- Cite a live endpoint, or invent, round, or estimate a value that isn't in an
  evidence file.
- Echo or write a token or credential.
- Commit `investigations/` to an upstream remote or an upstream PR.
- Change the question mid-study without the owner.
- Report only good news.
