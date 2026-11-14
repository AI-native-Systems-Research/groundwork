# Spotlights: static and telemetry runs (step 4), collecting them (step 5)

Read this only when the plan includes `spotlights-static` or
`spotlights-telemetry`. Every launch gets its own go; approving the plan does
not approve a launch. The two paths are gated differently for now:

- **spotlights-static** launches only through `scripts/runtime.py`
  (`runtime-protocol.md`): request, prepare, card, the owner's go quoting the
  short code, approve, launch.
- **spotlights-telemetry** is a **legacy gate, not runtime-enforced** (owner
  decision D10 holds its runtime adapter): a 2–3 sentence summary of what
  runs and what it costs, then its own go.

Paths below:

- `$SKILL`: the folder containing SKILL.md.
- `$TOP`: the target repo's git toplevel (the session repo; doctor prints it).
- `$STUDY`: `$TOP/investigations/<slug>/`.
- `$RUNS`: `$INVESTIGATION_RUNS/<repo>/<slug>/` (doctor prints `runs.root`).
  Spotlights output always goes to `$RUNS/spotlights-<id>/`, outside the repo
  and never committed. The study keeps only the parts it cites.

Facts below were checked against the spotlights checkout at `e7aff25`
(branch `skill/spotlights-run`; doctor warns that it is not the `v0.1.1`
release pin).

## spotlights-static

Three steps, each a go if it spends money:

1. `/spotlights-objective-setting`: agree the objective and workload hints
   with the owner. It writes an objective JSON under `<root>/objectives/`; use
   `$RUNS/spotlights-<id>/` as the root.
2. The engine run, through the runtime. Write an `experiment-request` with
   `capability: "spotlights-static"`, the agreed `objective`, and
   `semantics{include: [<module>, ...], models{claude, codex}, hints?}`
   (`runtime-protocol.md`, "Requests"). Module names are slash-form and must
   exist at the request's commit. Both model ids are required: the
   engine's models.yaml and `~/.claude` / `~/.codex` never decide them.
   `limits.max_cost_usd` is passed as `--max-cost`, which is **advisory**:
   the engine reports spend and never stops at it. Then `submit`, `prepare`,
   paste the card, wait for the go, `approve`, `launch`, `status`.

   The adapter always analyses the target checkout at the request's commit,
   with a clean tree, and writes to a fresh `$RUNS/spotlights-<prep-id>/`
   (artifacts in its `artifacts/`). It never resumes an existing run dir.
   Reuse an existing run's output when it already covers the module and the
   question; that needs no launch.
3. After `runtime.py collect`, `/spotlights-sort-candidates` on
   `$RUNS/spotlights-<prep-id>/result.json` (in-session, not a governed
   launch, decision D5). It writes `sorted_candidates.md` + `sorted_candidates.json` beside
   `result.json` or in its `sorted/` subdir (both are found by the collector).

What the engine writes (`--output-folder`):

| File | Shape |
|---|---|
| `result.json` | `{report, module_runs, per_module_telemetry, manager_issues, renderer_result, extractor_invocation}`. `report` is the `SpotlightReport` (`src/spotlights_engine/schemas/pipeline.py`): `{schema_version: "1", project_tree, context: {objective, workload_hints, validation_plan}, candidates[], findings[], anomalies[], run: {pipeline, run_id, started_at, finished_at, cost_usd}, issues[]}`. Each candidate (`schemas/candidate.py`): `{id: cand-<module>-NNNN, module_qualified_name, origin, locations: [{file, spans: [{line_start, line_end, symbol, kind}]}], description, current_approach, evolve_rationale, estimated_impact: high/medium/low, estimated_impact_explanation, proposals[]}`. |
| `run_manifest.json` | `{run_id, date, target: {repo_url, commit_sha, objective}, spotlights: {commit_sha, pipeline, config}, models_used, timing, cost, external_cost, outputs, total_tokens, notes}` (also at `<artifacts-dir>/spotlights_manager/run_manifest.json`). |
| `index.md`, `modules/…` | rendered pages; not kept in the study. |

## spotlights-telemetry (legacy gate, not runtime-enforced)

On the owner's go for this launch, as a background command:

```bash
signal-pipeline \
  --repo "$TOP" \
  --telemetry-from "$STUDY/telemetry/<id>/" \
  --output-folder "$RUNS/spotlights-<id>" \
  --artifacts-dir "$RUNS/spotlights-<id>/artifacts" \
  --to-stage 04
```

**Never run stage 05.** It is the execution stage and mutates the subject
repo (`signal_pipeline/cli.py`: "stage 05 (execution) mutates the subject repo
and is opt-in until it's worktree-isolated"). The CLI's default stop is
already 04; pass `--to-stage 04` anyway, and never `--from-stage 05`,
`--only-stage 05` or `--to-stage 05`, and never `--inject 05…`. Pass no
`--max-cost` expecting a cap; any cost figure is advisory. The signal
pipeline writes a bare `SpotlightReport` as `spotlight_report.json` under
**`--artifacts-dir`** (`$RUNS/spotlights-<id>/artifacts/`), not under
`--output-folder` as its README says. The collector does not look there by
itself, so give it `--run-dir "$RUNS/spotlights-<id>/artifacts"` and
`--sha <commit from question.md>` (it then copies the report as
`spotlights/result.json`; the sha is needed when no run manifest is found).

### Metrics-only caveat (F1)

Our captures are metrics only. Stage 01 accepts a `--telemetry-from`
directory only in the two forms the `stage01_inputs` fact records
(`upstreams/spotlights.json`; sources cited there): a pre-cooked Signals
JSON, or a raw-OTel directory recognised **only** by `traces.jsonl`.

A directory with just `metrics.jsonl` + `prom/` matches neither, so
`_resolve_signals_file` raises `FileNotFoundError`. The skill does
**not** work around this: it never writes a pre-cooked Signals JSON and
never fakes a `traces.jsonl`; anomaly detection is spotlights' job.
Until spotlights accepts a metrics-only directory, doctor reports
`spotlights-telemetry` as unavailable for metrics-only captures (its
reason string is built from the same fact), and `plan.md` says so. Even
once it works, expect fewer, coarser candidates (pod/metric level, not
function level) than the trace-based canonical runs; static Spotlights
on the module the metrics point at fills that gap. (Owner action,
still open: raise the gap with spotlights.)

## Collect (step 5)

spotlights-static: `runtime.py collect <inv-id>`, then `runtime.py result
<inv-id>`. Collection reuses the collector below and applies the outcome
rules: `candidates_found` and `no_opportunity` are completed executions with
outcome `other`; a run where no module succeeded, or zero candidates with
degraded modules or manager issues, is `failed`, never `no_opportunity`.

spotlights-telemetry (legacy) runs the collector directly:

```bash
python3 "$SKILL/scripts/collect_spotlights.py" --study "$STUDY" \
    --run-dir "$RUNS/spotlights-<id>" [--top 5] [--repo "$TOP"] [--sha SHA]
```

Read-only over the run dir. It:

- copies `result.json` (or `spotlight_report.json`), `run_manifest.json`,
  `sorted_candidates.md` and `sorted_candidates.json` into `$STUDY/spotlights/`
  (with their sha256 in the section). The `.md`'s links point at candidate
  pages in the run dir and don't resolve from the study copy;
- for the top-N candidates (rank from `sorted_candidates.json`, else the `.md`,
  else `result.json` order) cites the first span of the first location:
  `$STUDY/citations/cite-<candidate id>.txt` = header
  `repo@sha:path:Lstart-Lend`, then those lines exactly as
  `git -C <repo> show <sha>:<path>` prints them. The sha is the manifest's
  `target.commit_sha` (override with `--sha`), the repo defaults to the
  study's toplevel and its name to the origin remote's basename;
- drafts one claim per citation: `value` = the first non-blank quoted line,
  stripped; `condition` = `"<repo> @ <sha[:8]>, <path>:L<start>"`; `evidence`
  = `citations/<claim-id>.txt`; `verified: true`. Candidates whose span can't
  be cited (sha missing from the repo, path absent at the sha, lines out of
  range) get a note, no claim;
- merges the section under `collection.json["spotlights"]`, keeping the
  `nous` and `telemetry` keys.

Check citations before the answer: `validate_answer.py --check-citations`.
Citation claims are for **qualitative** questions only (set at intake).

## Proposals are hypotheses, never claims

A Spotlights candidate's `estimated_impact`, its explanation, sort scores and
rationales, proposals and their expected effects, and run costs are model
**estimates**. None of them ever becomes a claim value; only measured numbers
(Nous, telemetry) and verbatim code citations do. The collector lists the top
candidates' proposals under `candidates_top[].proposals_are_hypotheses`; the
answer may describe them in prose, labelled as proposals. To turn a proposal
into a measured claim, propose a Nous request whose
`target_system.description` cites the candidate id. That is a new step: a
step-log entry in `plan.md`, its own `next_step` block, its own card and its
own go. A Spotlights result never starts a Nous run by itself; stopping, or
asking the owner, are equally valid next steps.

## What to keep in the study

| Keep (`$STUDY/…`) | Leave in `$RUNS/spotlights-<id>/` |
|---|---|
| `spotlights/result.json`, `spotlights/run_manifest.json`, `spotlights/sorted_candidates.md` (+ `.json`) | `index.md`, `modules/`, `artifacts/` (checkpoints, transcripts), `evolve/`, `apply/` |
| `citations/<claim-id>.txt` for the claims the answer uses | citations the answer doesn't use (delete them) |
| `collection.json` (`spotlights` key) | — |
