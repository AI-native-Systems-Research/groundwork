# Method plan (steps 0–2)

Read this at preflight, intake and planning. The plan is a **provisional
method outline**, the gate between a question and any spend: nothing that
costs money or touches an external system runs until the owner approves
`plan.md`. Approving it grants no launch. Every launch still gets its own go,
and Nous and Spotlights static launches each get their own approval card
from `scripts/runtime.py` (`runtime-protocol.md`).

## Step 0: preflight

```bash
python3 $SKILL/scripts/doctor.py --json        # --offline to skip `git ls-remote`
```

`$SKILL` is the folder containing SKILL.md. doctor is read-only and stdlib-only.
It exits 0 unless the session isn't in a git repo. Use these fields:

- `methods.<name>.available` / `.reason`: the only methods the plan may pick,
  and the reasons to quote for the rest.
- `engines.<name>`: found, version/sha, pin (from SKILL.md frontmatter), latest
  upstream release, and `warnings` (installed differs from the pin or the latest
  release, or the engine sits on an unpushed branch). Tell the owner about every
  warning. A study never pins or cites an unpushed engine commit.
- `repo`: toplevel, remotes, which one is upstream, and the push remote.
  `push_is_upstream: true` means `new_study.py --commit-guard` will refuse.
- `runtime`: the governed-launch runtime's version, the DB schema version it
  reads, and the process start-time method. If `runtime.available` is false,
  `nous-blis` and `spotlights-static` are unavailable too, with the reason.
- `unconfigured`: what the machine config is missing (BLIS apparatus root,
  engine checkouts). Engines fall back to PATH when they aren't configured.

Machine config (`~/.config/investigation/config.yaml`, or
`$INVESTIGATION_CONFIG_HOME/config.yaml`):

```yaml
runs_dir: ~/.cache/investigation           # $INVESTIGATION_RUNS overrides it
blis_apparatus_root: /path/to/blis-apparatus
engines:
  nous: /path/to/agentic-strategy-evolution
  spotlights-engine: /path/to/spotlights
  signal-pipeline: /path/to/spotlights     # ships inside spotlights; defaults to spotlights-engine's path
  sim2real: /path/to/sim2real
repos:                                     # for validate_answer.py --check-citations
  vllm: /path/to/vllm
```

Never write this file without the owner; it's theirs.

## Step 1: intake

```bash
python3 $SKILL/scripts/new_study.py --slug <slug> --question "<question>" [--tracking fork|untracked]
```

It creates `investigations/<slug>/question.md`, `plan.md` and `hand-score.md`,
with the remote URL, branch, HEAD sha and engine versions pinned in
`question.md`. On the first study in a repo it needs a tracking mode. Ask the
owner, then pass `--tracking`:

- **fork** (default): writes `investigations/config.yaml`, committed on the
  owner's fork only.
- **untracked**: writes `~/.config/investigation/repos/<repo>.yaml` and adds
  `investigations/` to `.git/info/exclude`. Studies stay on disk only.

Then fill in `question.md`:

- the question in your words, and where it came from (link it);
- **kind**: qualitative ("where / why / how does X work") or quantitative
  ("how much / does X beat Y"). Code citations are allowed as claims for
  qualitative questions only;
- the falsifiable form ("this is wrong if…"), with metric, threshold and
  comparator fixed before anything runs;
- the baseline, usually the shipped default ("B-ship"), named before any run;
- the known limits that must appear in the eventual answer.

Before drafting, check prior learnings: grep earlier `investigations/*/` in
this repo, and the `principles.json` files under the runs dir
(`$INVESTIGATION_RUNS/<repo>/*/*/principles.json`). There is no Nous v2
principle-search tool; grep is the tool. If a prior study already answers the
question, that's a pushback (below).

## Step 2: the plan

Fill `plan.md` (scaffolded from `assets/plan.md.tmpl`):

| Section | What goes in it |
|---|---|
| Methods chosen | Any of `code-reading`, `telemetry-snapshot`, `spotlights-static`, `spotlights-telemetry`, `nous-blis`, `sim2real-prep`, and only ones doctor reports available |
| Why each, why not the others | One line per method, including the rejected and unavailable ones (quote doctor's reason) |
| Likely order | e.g. telemetry → spotlights-static on the module the metrics point at → nous-blis tests a fix. Provisional: each step after the first is decided in the step log |
| Telemetry window | Cluster name, absolute RFC 3339 start/end, step, and why (see `telemetry.md`) |
| Cost and time | Rough $, wall-clock and runs-dir disk per method. Money and time figures are estimates; no engine caps them |
| Launch gates | Which launches go through the runtime (nous-blis, spotlights-static) and which are legacy gates, not runtime-enforced (telemetry-snapshot, spotlights-telemetry) |
| Claim kinds | Measured only, or measured + code citations |
| Pushback | If any (below) |

Show it to the owner and wait. Approval of the plan is not a go for any launch.

### Step log (after every result)

The plan ends with a `## Step log`. After each method's result, add an entry:

- **Interpretation** of the result, and its outcome classification.
- **Remaining uncertainty.**
- **Next step or stopping reason.** Stop when the answer is good enough or
  nothing affordable would change it; stopping needs no launch. Ask the owner
  when the question itself is unclear. Otherwise propose **one** next step.
- **Expected information gain** of that step: what it could change in the
  answer.
- **Limits** that apply (iterations, advisory cost and time).

A proposed next step is a new request whose `next_step` block is copied from
this entry (`interpretation_of_prior`, `remaining_uncertainty`,
`expected_information_gain`, `rationale`), with its own card and its own go.
The runtime checks the block is there; whether the step is worth it is for
you and the owner. A result never starts the next method by itself.

### Method menu

| Method | Good for | Rough cost | Output |
|---|---|---|---|
| `code-reading` | "Where is X / what's the default" | ~free, minutes | code citations, `grounding.md` |
| `telemetry-snapshot` | "What does cluster X actually do" | ~free, minutes | frozen metrics files under `telemetry/<id>/` |
| `spotlights-static` (one or more modules + an objective; runtime-governed) | Mechanism/policy questions in real code; "what would improve X" | ~$3–5, ~1 h per module | candidates + proposals with file:line |
| `spotlights-telemetry` (a telemetry dir; legacy gate) | Hot spots that only show up under load | ~$2, ~10 min | candidates tied to observed anomalies |
| `nous-blis` (runtime-governed) | Controlled "does X beat Y" over knobs BLIS represents | hours, tens of GB of runs | measured claims |
| `sim2real-prep` | After a Nous win you might want to validate for real | minutes | transfer bundle (never deployed) |

Notes:

- **code-reading** is always the floor. Grounding (where the lever lives in the
  real code, plus its shipped default) goes in `grounding.md` for every study,
  even when another method does the measuring.
- **spotlights-static vs code-reading.** Use Spotlights when the question is
  about a mechanism or policy in the real code and a structured search over a
  module is worth ~$3–5. Reuse an existing module output when one covers the
  question. Skip it for a numeric sweep over a documented knob; then grounding
  is a code search plus the shipped default, and `grounding.md` records why
  Spotlights was skipped.
- **spotlights-telemetry is unavailable for metrics-only captures.**
  signal-pipeline's stage 01 only takes raw OTel when `traces.jsonl` exists, and
  otherwise needs a pre-cooked `01_signals.json`. Anomaly detection is
  spotlights' job, so this skill doesn't write `01_signals.json` and doesn't
  fake a `traces.jsonl`. It is usable only with an `import` capture that already
  holds one of those. With metrics only, plan static Spotlights on the module the
  metrics point at instead.
- **nous-blis** needs Nous v2, a configured BLIS apparatus, and a question BLIS
  can represent (next section). Details: `nous-blis.md`.
- **sim2real-prep** only follows a Nous win. Details: `sim2real.md`.

### BLIS representability

<!-- generated from references/upstreams/blis.json by scripts/upstreams.py; edit that file, not this table -->
| Representable | Not representable |
|---|---|
| KV offload knobs (`--kv-cpu-blocks`, `--kv-offload-threshold`, `--kv-offload-config`) | EPP scorer weights |
| KV-event delay (`--kv-event-delay`) | batch gateway |
| Speculative indexing and its TTL (`epp:precise-prefix-cache-producer.speculativeIndexing`, `.speculativeTTL`, set in the scenario's EndpointPickerConfig) | WVA autoscaling |
| | RL time-slicing |
| | wide-EP |
| | approximate-prefix LRU, `maxPrefixTokensToMatch` |

If the question turns on something BLIS can't represent, the plan **excludes**
`nous-blis` and says why. That doesn't end the study (other methods may fit),
and BLIS is never quietly approximated. When the repo config says
`blis_applicable: false`, doctor reports `nous-blis` unavailable.

### Pushback

The plan may conclude that none of the methods fit, and say so instead of
forcing one. A study that ends at step 2 with a pushback has a result. Typical
reasons:

- **Not falsifiable as posed** → propose a re-posing and let the owner choose.
- **Needs data we can't get** (traces when only metrics exist, a cluster we
  can't reach, a knob BLIS can't represent) → say which data, and what would
  unlock it.
- **Already answered** by a prior study → point at it.
- **A pure lookup** → answer with `code-reading` alone and say that's all it
  needed.
- **Cost out of proportion** to the decision it informs → say so with the numbers.

### Cost table (rough, for the plan)

| Method | $ | Wall-clock | Disk |
|---|---|---|---|
| code-reading | ~0 | minutes | ~0 |
| telemetry-snapshot | ~0 | minutes per capture | MB; > 50 MB moves to the runs dir |
| spotlights-static | ~$3–5 per module (`--max-cost` is advisory, not a cap) | ~1 h per module | runs dir, MB |
| spotlights-telemetry | ~$2 | ~10 min | runs dir, MB |
| nous-blis | model tokens for the session + compute | hours (a full A1-scale sweep was ~4 h) | tens of GB (A1 full was ~60 GB) |
| sim2real-prep | ~0 | minutes | KB |

Give the owner the numbers for *this* plan, not the table.
