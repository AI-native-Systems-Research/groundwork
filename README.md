# groundwork

> [!NOTE]
> **Experimental.** This is a research prototype we're still shaping, so
> interfaces and file formats change often and refactors can be large.
> Pin a commit if you need the same behaviour twice.
> See [Status](#status) for what's in flux.

[AI-native Systems Research](https://github.com/AI-native-Systems-Research)
and [inference-sim](https://github.com/inference-sim) have built many tools:
Nous for controlled experiments, Spotlights for code and telemetry
discovery, the BLIS simulator, sim2real, and more. groundwork puts them to
work on real engineering questions about systems like llm-d and vLLM, such
as "Is the shipped P/D threshold still the right default?"

It does this through `investigation`, a skill for your coding agent. You ask
a question. The agent picks the tools that fit, gets your OK before anything
expensive runs, and writes an answer where **every number points at a file
that provably contains it**.

## Status

We're still working out what this should be. The ideas below are the settled
part; the code around them is not. Specifically:

- **Interfaces aren't stable yet.** Script names, flags, and outputs change,
  as do the skill flow, the `investigations/<slug>/` layout, the answer
  contract, and the runtime's submit → prepare → approve → launch protocol.
- **Refactors can be large.** Steps may get merged, split, or dropped as we
  learn what's actually useful.
- **No releases or versioning yet.** No semver, no migration notes. `main` is
  the branch to use, and it moves.
- **Older investigations may need a re-run.** Results written by an earlier
  version won't always validate against a later one.
- **Still early on coverage.** It's been used on a handful of questions across
  a few repos, so expect rough edges.

Using it now works well if you pin a commit and keep your own copy of results
you care about. Bug reports and sharp feedback are exactly what's useful at
this stage.

## The mission

Make it cheap to get a grounded answer, and easy to tell a grounded answer
from a guess. Four ideas drive everything here:

1. **Evidence or it didn't happen.** Every claim in an answer names its
   evidence: a result file, a telemetry snapshot, or a line of code pinned to
   a commit. `validate_answer.py` checks that each file exists and contains
   the claimed value.
2. **Pick the method to fit the question.** Not every question needs a
   simulator. Some need a careful code read. Some need live metrics. The agent
   plans which methods fit, explains why, and pushes back if none do.
3. **A human approves every expensive step.** Nothing that costs real time,
   compute, or model spend runs until you say "go" for that specific launch.
   A small local runtime enforces this for the heavy tools (more on that
   below).
4. **Negative results are results.** "No, the default still wins" is a valid
   answer. The agent never changes the question or tunes things after seeing
   the results.

## What an answer looks like

Each investigation lives in your target repo under
`investigations/<slug>/` and ends in `final/answer.json`. Here is a trimmed
example from [examples/pd-threshold-retro](examples/pd-threshold-retro/):

```json
{
  "answer": "No. A fitted per-layout rule for the P/D threshold does not beat the shipped T=16 as a deployable rule. ... Keep 16.",
  "claims": [
    {
      "id": "c2",
      "metric": "guided_capacity_gain_pct_vs_t16",
      "value": 29.7,
      "condition": "W5t (toolagent trace), 2P/6D layout, fitted rule t8192 vs shipped t16, seeds 1-3",
      "evidence": ".../results/a1/analysis/....json"
    }
  ]
}
```

A short answer in prose, then claims. Each claim says what was measured,
under what conditions, and where the proof is. The full format is the
[answer contract](skills/investigation/references/answer-contract.md).

## How it works

```
 you ask a question
        │
        ▼
 1. intake      the agent records the question and the repo's exact commit
 2. doctor      it checks which tools are installed on this machine
 3. plan        it picks methods, explains why, estimates cost  ──►  you approve
 4. run         each expensive launch needs its own "go"        ──►  you say go
 5. answer      it drafts answer.json and shows it to you
 6. validate    a script checks every claim against its evidence
 7. score       you hand-score the answer for quality
```

The methods the agent can choose from:

| Method | Good for | Needs |
|---|---|---|
| **Code reading** | "Where/why does X happen?" Citations are pinned to exact lines and commits. | Nothing extra |
| **Telemetry snapshot** | "What does our real traffic look like?" A frozen copy of Prometheus metrics. | `kubectl` or a Prometheus URL |
| **[Spotlights](https://github.com/AI-native-Systems-Research/spotlights)** | Finding likely hot spots or improvement ideas in a codebase, from code alone or guided by telemetry. | `spotlights-engine` |
| **[Nous](https://github.com/AI-native-Systems-Research/agentic-strategy-evolution) in the BLIS simulator** | "Does policy A beat policy B?" Controlled experiments in a serving simulator. | Nous v2 and a BLIS setup |
| **[sim2real](https://github.com/inference-sim/sim2real) prep** | Getting a simulator finding ready to test on a real cluster. Prepared, never deployed. | A sim2real checkout |

### The runtime

Nous and Spotlights can run for a long time and cost real money, so they
don't get launched directly. They go through a small local runtime
([`scripts/runtime.py`](skills/investigation/scripts/runtime.py)) that works
in four steps: submit → prepare → approve → launch. The runtime shows you an
approval card with a short code. It will only launch once your "go" for that
card is recorded. It also tracks status and handles cancel and result
collection. The runtime design comes from the AINS (AI-native systems)
runtime spec in [docs/ains-runtime-spec.md](docs/ains-runtime-spec.md).

## Install

The skill installs into the repo you want to investigate, not this one:

```bash
cd ~/git/llm-d-router                    # the repo you want to investigate
npx skills add AI-native-Systems-Research/groundwork --skill investigation -a claude-code   # → .claude/skills/investigation/
npx skills add AI-native-Systems-Research/groundwork --skill investigation -g               # → ~/.claude/skills/ (all repos)
```

The repo is private for now, so you need GitHub access to it. To install
from a local checkout instead, swap in its path (e.g. `/path/to/groundwork`).

Installing again later can pick up interface changes, so pin a commit if you
need the same behaviour twice.

Requires Python 3.10+ and PyYAML (`pip install pyyaml`). Each method needs its
own tools (see the table above). `doctor.py` reports which ones you have, and
the agent only plans methods you can actually run.

## Quickstart

1. `cd` into the target repo and start your agent there.
2. Ask a question, or run `/investigation "does the 16-token block size still hold?"`.
3. The skill runs `scripts/doctor.py` to see which methods are available here.
4. It writes `investigations/<slug>/plan.md` (methods, reasons, cost, any
   pushback) and waits for you to approve it.
5. It runs the approved methods, asking for a "go" before each expensive one.
6. It drafts `final/answer.json`, shows it to you, and runs
   `validate_answer.py` on it (`--check-citations` for code citations). Then
   you hand-score it with `references/hand-scoring.md`.

## What's in this repo

This repo is where the skill is built and shipped from. Only
`skills/investigation/` gets installed.

```
skills/investigation/      the installable skill
  SKILL.md                 the rules and the step-by-step flow the agent follows
  scripts/                 doctor, new_study, runtime, collectors, validate_answer, ...
  references/              per-step detail, loaded only when needed
  assets/                  templates and the starter PromQL list
tests/                     unit tests for the scripts (not installed)
examples/                  worked investigations (see examples/README.md)
docs/                      design docs, specs, build log (not installed)
scripts/ci-local.sh        run the CI checks locally (not installed)
```

### Where to read next

- [skills/investigation/SKILL.md](skills/investigation/SKILL.md): exactly what
  the agent does, and the hard rules it must follow.
- [docs/design-v2.md](docs/design-v2.md): why the skill plans methods instead
  of running one fixed pipeline.
- [docs/runtime-integration-spec.md](docs/runtime-integration-spec.md): how
  the approval runtime fits under the skill.
- [examples/pd-threshold-retro](examples/pd-threshold-retro/): a complete
  worked investigation, from question to hand score.

## Credits

- **The answer contract** comes from the mlsys study by
  [mtoslalibu](https://github.com/mtoslalibu) (`experiments/mlsys/tasks/contract.md`
  on the `mlsys-exp` branch of
  [agentic-strategy-evolution](https://github.com/AI-native-Systems-Research/agentic-strategy-evolution)).
  Nous v2's `answer.schema.json` independently adopts the same shape.
- **Nous**: [agentic-strategy-evolution](https://github.com/AI-native-Systems-Research/agentic-strategy-evolution), v2.
- **Spotlights** and its **signal-pipeline**: [spotlights](https://github.com/AI-native-Systems-Research/spotlights).
- **sim2real**: [inference-sim/sim2real](https://github.com/inference-sim/sim2real).

## License

Apache-2.0. See [LICENSE](LICENSE).
