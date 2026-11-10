# sim2real prep (step 8, optional)

Read this only when `plan.md` lists `sim2real-prep` and a Nous campaign has a
winner the owner wants to check on a real cluster.

sim2real (`github.com/inference-sim/sim2real`) promotes a simulator result into
a real llm-d deployment. This skill **prepares** its input and stops there. It
never deploys.

Facts below were checked against sim2real `main` @ `28a8e98` (2026-11). If the
local checkout is newer, re-check the command shapes in its
`pipeline/README.md` ("Register a translation") and
`.claude/skills/sim2real-bootstrap/SKILL.md` before printing anything.

## The hard line: never deploy

The skill never runs any of these, even with a "go":

- `pipeline/deploy.py` (any subcommand: `run`, `collect`, `wipe`, ...)
- `pipeline/cluster.py` (`init`, `provision`, `slot add`)
- `sim2real assemble`, `sim2real build`, or `translation register --build`.
  `--build` dispatches buildkit on a provisioned cluster.
- Anything that applies Tekton resources or touches a kube context.

All of that is owner-run and outside this skill. What the skill does: write
files under `investigations/<slug>/sim2real/`, then **print** the next
command for the owner. It doesn't run that command either. The owner runs it
from the sim2real checkout.

## Which bundle: algorithm vs knob

| Campaign kind | Example | Bundle | Owner's next step |
|---|---|---|---|
| **Algorithm**: Nous evolved new Go code (a scorer, a decider) | an evolved P/D decider | BLIS-mode bootstrap folder | `/sim2real-bootstrap` in that folder |
| **Knob**: the winner is a flag or config value on the shipped EPP, with no new code | block size, P/D threshold | `--byo` overlay | `/sim2real-bootstrap --byo ...`, then the printed `translation register` |

If the winner needs new code *and* a flag change, treat it as an algorithm
campaign. A BYO overlay can only change config on an image that already exists.

## Algorithm bundle (BLIS mode)

`/sim2real-bootstrap` (BLIS mode, the default) requires exactly this shape in
the experiment folder:

```
investigations/<slug>/sim2real/
  algorithms/*.go        the winning arm's evolved source, copied verbatim from the Nous work dir
  workloads/*.yaml       the campaign's workload definitions (the same files the arms ran)
  config.md              the locked arm configuration as markdown tables
```

- `algorithms/` and `workloads/` are hard prerequisites. Bootstrap exits
  without them.
- The config source can be `config.md`, `config.yaml`, or JSON
  (`top3_selection.json`). Use `config.md`. Bootstrap's
  `generate_from_config.py` parses its markdown tables (the vLLM
  configuration table, hardware, model, the `blis observe` block) into
  baseline scenarios. Copy the values from the campaign's locked arms and
  `question.md` pins. Don't retype them from memory.
- Put a `README.md` in the folder recording the source: campaign path, winning
  arm id, iteration, and the sha256 of each copied `.go` file.

Then print, for the owner:

```
Next (owner, in a Claude session inside sim2real):
  /sim2real-bootstrap <absolute path to investigations/<slug>/sim2real>
```

Bootstrap asks the owner to approve each step and derives `transfer.yaml`,
`baselines/`, and a component submodule. That is sim2real's process, not ours.

## Knob bundle (`--byo` mode)

A knob winner runs on the **shipped EPP image**. The treatment differs from the
baseline only in the winning flag. The baseline is **B-ship**: the shipped
default the campaign compared against (e.g. block size 16).

Write:

```
investigations/<slug>/sim2real/
  baseline.yaml               full llm-d-benchmark-style scenario for B-ship (shipped image, shipped flags)
  overlays/<name>.yaml        partial-YAML treatment overlay: only the winning flag/config differs
  README.md                   image ref (+ digest if known), winning arm, which flag changed, source pins
```

- `<name>` is the algorithm name sim2real will use. It must match
  `[A-Za-z0-9][A-Za-z0-9._-]*`, at most 128 chars. `baseline`, `baselines`,
  `default` and `defaults` are reserved. Use something like `blocksize-32`.
- The image ref is the shipped EPP image for the release the campaign
  modelled. Prefer a digest ref (`image@sha256:...`).
- Overlay paths must not contain `@`, because the register spec splits on the
  rightmost `@`.

Then print the two owner commands. The first scaffolds the experiment repo
with sim2real's own BYO bootstrap. It copies the files to canonical locations
and writes `transfer.yaml` with `byo: true`:

`byo.py` treats the **current directory** as the experiment root and writes
into it. So the owner should `cd` into a fresh experiment folder first. That
can be the study's `sim2real/` folder, or a separate experiment repo. It
should not be the target repo's root.

```
cd <experiment root>
python3 <sim2real>/.claude/skills/sim2real-bootstrap/byo.py --byo \
    --baseline <study>/sim2real/baseline.yaml \
    --algorithm <name> \
    --algorithm-image <name>=<image-ref> \
    --algorithm-config <name>=<study>/sim2real/overlays/<name>.yaml
```

At the end, `byo.py` prints the register command. It looks like this:

```
cd '<abs experiment root>'
sim2real translation register \
    --algorithm '<name>=<image-ref>@algorithms/<name>/<name>_config.yaml'
```

### The register command: what's actually real

The design says "print the `sim2real translation register` command". Checked
against the source:

- **Spec form.** The current form is `--algorithm NAME=IMAGE@CONFIG`,
  repeatable. `--algorithm NAME --image REF --config PATH` still parses but is
  **deprecated** (N=1 only, emits a warning). Don't print the deprecated form,
  even though `pipeline/setup.py`'s "next steps" text still shows it.
- **No `sim2real` executable.** sim2real isn't an installable package
  (`pyproject.toml` is declaration-only). The bare `sim2real translation
  register` that `byo.py` prints is shorthand. The command that actually runs,
  from the sim2real checkout, is:

  ```
  python pipeline/sim2real.py --experiment-root <abs experiment root> \
      translation register \
      --algorithm '<name>=<image-ref>@algorithms/<name>/<name>_config.yaml'
  ```

  `--experiment-root` is a **top-level** flag. It must come before
  `translation`, and argparse rejects it after `register`. Without it,
  sim2real uses the cwd, which is why `byo.py` prints `cd` first.
- Optional flags: `--baseline-config PATH` (baseline overlay),
  `--registered-hash HASH` (assert the hash), `--force` (reassign an alias).
- Python 3.12+ and PyYAML are required. `pipeline/__init__.py` refuses older
  interpreters.
- `register` with `--algorithm` only writes under
  `<experiment root>/workspace/translations/<hash>/`. It may contact the image
  registry to resolve a digest. It still belongs to the owner: print it, don't
  run it.

After `register`, the owner continues with `sim2real assemble`,
`deploy.py run`, and `deploy.py collect`. Those steps are out of scope, so
mention them in one line and don't spell them out.

## What comes back: a follow-up study

If the owner deploys, `deploy.py collect` pulls results into the sim2real
workspace, per cell:

```
workspace/runs/<run>/results/<phase>/<workload>/i<N>/metrics/raw/<pod>_<ts>_metrics.log
workspace/runs/<run>/results/<phase>/<workload>/i<N>/metrics/processed/metrics_summary.json
```

- `metrics/raw/*_metrics.log` holds Prometheus text-exposition dumps, one per
  pod per scrape (15 s by default), unfiltered, so every exposed metric is
  there.
- `metrics/processed/metrics_summary.json` holds post-run percentiles, but
  only for the metrics in llm-d-benchmark's `process_metrics.py:AGGREGATE_METRICS`.
  A metric missing from the summary may still be in the raw logs.

These come into a **new** study through the telemetry `import` source (see
`references/telemetry.md`), copied in as-is under `telemetry/<id>/`. That
follow-up study compares BLIS's prediction (the original study's
`final/answer.json` claims) with what the cluster measured, under the same
answer contract. The original study isn't edited: it already has its answer.

## Recording it

- `plan.md` lists `sim2real-prep` with its cost ("minutes, files only").
- `collection.json` doesn't change. sim2real prep produces no measured
  numbers.
- `final/answer.json` can mention the prepared bundle in `deliverable`. A
  sim2real bundle is never evidence for a claim. Only the follow-up study's
  imported metrics can be.
