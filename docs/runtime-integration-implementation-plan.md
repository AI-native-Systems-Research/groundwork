# AINS local runtime integration — implementation plan

Status: Accepted and implemented. The owner accepted the spec and this plan on
2026-11-09 ([runtime-integration-spec.md](runtime-integration-spec.md) §Status,
cited below as "spec §N"). Milestone A shipped that day for Nous and Spotlights
static; the telemetry phases (R6a/R6) are held under owner decision D10 and
R9's real runs still await the owner's approval, so Milestone A is not yet
complete — the phase status markers below record what shipped and what
stayed open. `ains_runtime/` and `scripts/runtime.py` are on `main`, and later
PRs have extended them since.

This plan breaks Milestone A into eleven PR-sized phases (R0–R9, plus R6a) and Milestone B
into two (B1–B2). Each phase leaves the branch green: `scripts/ci-local.sh`
passes and the skill installs. Section 1 records the spec §15 verification
answers. Section 2 lists places where the spec, the current skill and the
pinned engines disagree. Section 7 records the owner decisions.

**Where the engine facts come from.** Facts about Nous and Spotlights come
from a read-only review of the pinned source. Nous was read at `e224cc1`
(`nous-v2-fixes`, in `~/git/agentic-strategy-evolution`). Spotlights was read
at `cccc290` (`v0.1.1`, in `~/git/spotlights`). Both were read with
`git show <pin>:<path>`, so the working trees, which sit at other commits,
were never used. The installed CLIs (`nous` 0.5.0, `spotlights-engine` and
`signal-pipeline` 0.1.0) match those pins. `path:line` references are at the
pin. R0 (done, §1.7) re-confirmed every fact the design depends on before code is written
against it. If a fact is marked **not established**, the plan must not depend
on it.

---

## 1. Spec §15 verification answers

### Q1. What owner-facing approval mechanism is available in each supported agent host?

Finding: in the Claude Code VS Code extension, the agent's Bash tool has no
TTY (`os.isatty(0)` is False, and `/dev/tty` gives "Device not configured").
A terminal prompt therefore can't be the common path.

**Decision (D1, owner): approval happens in chat, through the agent.** The
owner's interface is the skill running in their agent (Claude Code, Codex,
and so on). Every owner interaction, approval included, goes through that
chat. This works the same way in every host. The mechanism:

1. `runtime.py prepare` prints an **approval card**: the material contents,
   limits, inferred defaults, and the full `material_digest` plus a 12-hex
   short code.
2. The agent shows the card to the owner verbatim, with nothing left out,
   **in its own reply text**. Tool output alone is not enough: the host
   shows Bash stdout to the agent, but not reliably to the owner, and long
   output may be collapsed (R0). `prepare` also writes the card to
   `prepared/<prep-id>/card.md`, so the owner can open it if the relay
   is in doubt.
3. The owner replies in chat with the go. The skill asks the owner to quote
   the short code, so an approval can't get attached to the wrong
   preparation.
4. The agent runs
   `runtime.py approve <prep-id> --code <short> --source chat --quote "<owner's reply>"`.
   The runtime checks that the code matches the digest. It records the
   approval with its source, the quoted reply and a timestamp, bound to that
   one digest, and the approval can be used once.

What it doesn't do (stated in the protocol doc): it **records** consent the
owner gave in chat. It cannot prove that a human gave it. An agent that
ignores the skill could fabricate an approval. Binding to the digest still
gives the protections that matter: an approval can't be reused for a changed
run or for a second run. This replaces the spec's "an agent-written boolean
is insufficient" (spec §7) with: "an approval must quote the prepared digest
and is recorded with its chat source." **Spec §7 needs that amendment.** A
stronger out-of-band bridge (a terminal or a signed approval) is possible
later but is not planned.

### Q2. Which exact native states identify completed, stopped, resumable, and incomplete Nous runs?

Nous has **no status field and no single terminal marker**:

- `state.json` holds `phase` ∈ `BASELINE|DESIGN|EXECUTE|REPORT`. `phase` is set to REPORT when REPORT *starts* (`orchestrator/v2/loop.py:508`).
- The stop reason is in the **last `ledger.json` row**: `stop_reason` ∈ `null|halt|stop_sentinel|answered|max_iterations|stall` (`ledger_entry.schema.json:19`).
- The no-baseline path is different. It writes `stop_reason: "no_baseline"` to `state.json` and leaves the ledger empty (`loop.py:346-347`).
- On any completion path, `final_check.json` is **written last** (`loop.py:515-520, 586`). It is written even when `answer.json` is invalid or missing.
- `halt.json` appears after three identical failure fingerprints (`robustness.py:33,48`). REPORT still runs after it (`loop.py:495-501`). When the halt comes from a DESIGN or EXECUTE exception, the loop breaks **before** that iteration's ledger row is written (`loop.py:387-389, 419-421`), so the last ledger row does not say `halt`. Only `halt.json` shows it.
- Exit codes are 0 (normal return), 1 (any exception, including preflight) and 130 (KeyboardInterrupt) (`cli.py:483-495`). The exit code does **not** carry the stop reason.
- Anything with a `state.json` can be resumed (`resume.py:18-20, 46-50`). A run interrupted while in REPORT reruns only REPORT (`loop.py:352-355`).

The adapter uses these rules to classify a Nous run. The rows are checked
**in order**, and the first match wins. `halt.json` comes first because the
ledger may not record a halt:

| Native evidence | Runtime state | Notes |
|---|---|---|
| process alive (pid and start time match) | `running` | |
| `halt.json` present (REPORT may still have run) | `failed`, any findings labelled `partial` | decision D3 |
| `final_check.json` present, last ledger `stop_reason` ∈ {answered, max_iterations, stall} | `completed` | `native_stop_reason` recorded |
| `final_check.json` present, `stop_reason = stop_sentinel`, and the runtime recorded a cancel request | `cancelled` | Nous ran REPORT, so the output may be partial |
| `final_check.json` present, `stop_reason = stop_sentinel`, no cancel request recorded | `completed` + warning | Someone wrote STOP outside the runtime |
| `state.json.stop_reason = no_baseline` | `failed` (reason `no_baseline`) | no measurement exists |
| process exited, `state.json` present, no `final_check.json` | `failed` (resumable) | covers exit 1 and 130 |
| `final_check.json` present, but no row above matches (for example, an empty ledger with no `no_baseline`) | `failed` (reason `unclassified_native_state`) | never `completed` by default |
| supervisor or process identity lost, no terminal evidence | `recovery_required` | spec §8 |

The engine's exit code is never enough to mark a run completed. Neither is
the existence of `answer.json`.

### Q3. Which limits, stop operations, and cost reports do the pinned engines support?

| Limit or operation | Nous `e224cc1` | Spotlights static (`spotlights-engine`) | Spotlights telemetry (`signal-pipeline`) |
|---|---|---|---|
| Iteration/run bounds | `max_iterations` (default 5, `config.py:63`) is **engine-enforced** (`loop.py:358`). Stall stop after 2 iterations without improvement (`config.py:72`). 3-strike failure breaker (`robustness.py:33`). `--max-iterations` CLI override (`cli.py:146-147, 412`). | none at run level | none at run level |
| `task_limits.max_runs` | A **validation error**, not a stop (`validators.py:602-605`): once repairs run out, the iteration is marked `partial` | — | — |
| Cost cap | **none**: cost is reported only | `--max-cost` is **advisory only**: it is validated and printed, but never enforced (`cli.py:839-840, 880-881`; help text says "Advisory", `cli.py:319`; no other reference to `max_cost` under `src/`) | no cost flag |
| Cost report | `cost_meter_usd` for each ledger row and `report_cost_usd`. **Incomplete:** BASELINE cost is folded into iteration 1, a crashed iteration has no row, the no-baseline path records nothing, and models missing from the price table count as $0 (`cost.py:58-60`) | `run_manifest.cost` / `external_cost`. Partial when `priced_token_share < 1` (`costing/manifest.py:65-75`) | sum of per-stage `total_cost_usd`. Stage 02 records none. `None` if no stage recorded cost |
| Wall clock | none at campaign level. Per-iteration `wait_for` ≈ 4 h (240 polls × 60 s, `robustness.py:101, 124`) | per-call timeouts only (for example `candidate_discovery/api.py:54`, 900 s). Step 3 Codex is unbounded by default (`module_deep_research/codex_exec.py:38`, `timeout_seconds=None`) | per-stage timeouts 1800/1800/900 s for stages 01/03/04 (`s01_signal_extraction.py:78`, `s03_candidate_generation.py:37`, `s04_change_generation.py:37`). On timeout the child's process group is killed with SIGKILL (`_subprocess_util.py:115, 314`) |
| Stop | `nous stop` writes a `STOP` file (`cli.py:222-238`). It is checked **only after an iteration's EXECUTE phase and ledger row** (`loop.py:479-493`, `stop_rule.py:103`), then REPORT runs anyway (`loop.py:501`). Ctrl+C/SIGINT exits 130 and the run stays resumable (`cli.py:485-487`). **No SIGTERM handler** (no `signal.signal` anywhere under `orchestrator/`). | **no stop mechanism**. No signal handlers (no `signal.signal` under `src/`). Step 1 (modules extractor) spawns `claude` with `start_new_session=True` (`modules_extractor/agent.py:164`, `claude_stage.py:400` → `_subprocess_util.py:249`). Steps 2–5 use plain `subprocess.run`/`Popen`, so those children stay in the engine's group. | none. **Every** `claude` child runs in its own session (`claude_subprocess.py:157` → `_subprocess_util.py:249`). A signal to the engine's process group does **not** reach them. R0 confirmed this empirically (§1.7) |
| Resume | `nous resume` | on by default, keyed on `--artifacts-dir` (`cli.py:130, 286`). Refused if the input fingerprint changed (`spotlights_manager/orchestrator.py:578`) | on by default. Skips completed stages (`runner.py:404, 455`) |

What this means for the runtime:

- **Runtime-enforced:** every Nous request must carry `max_iterations`;
  one approval per invocation; no duplicate equivalent invocations. There
  are no invocation-count caps (D8).
- **Engine-enforced and recorded as such:** Nous `max_iterations` and the
  stall stop, and the engines' per-call timeouts.
- **Advisory:** every monetary figure and every wall-clock figure. Unknown
  spend is reported as `unknown`, never as 0.

### Q4. Which native artifacts support each normalized finding and limitation?

**Nous.** Hypothesis classification lives in `runs/iter-N/findings.json`.
Each hypothesis has `status` ∈ `CONFIRMED|PARTIALLY_CONFIRMED|REFUTED|INCONCLUSIVE`
and `error_type` ∈ `direction|magnitude|regime|shape|none` (`findings.schema.json:17, 22`). CONFIRMED and
REFUTED require |effect| > 2×noise_sd (`validators.py:461-498`). That check is skipped when noise is
zero or `noise_unmeasured: true` (`validators.py:471, 482`). The
hypothesis being classified is defined in `runs/iter-N/bundle.json`
(`mechanism`, `prediction{metric, direction}`, `falsified_if`).
`answer.json` carries contract claims and has **no** support/refute field.
**No artifact has a `limitations` field** (no match for `limitations` under `orchestrator/v2/schemas/`).

| Normalized field | Source |
|---|---|
| `hypotheses[].statement` | `bundle.json` `hypotheses[].{mechanism, prediction, falsified_if}` (the locked copy, `.bundle.locked.json`, is preferred) |
| `hypotheses[].classification` | `findings.json` `status`: CONFIRMED→`supported`, REFUTED→`not_supported`, INCONCLUSIVE→`inconclusive`, PARTIALLY_CONFIRMED→`other` + `partial_note` (decision D2) |
| `outcome.classification` (study level) | none native. `unavailable` with a reason, unless the coordinator supplies one tied to the request's stated hypothesis (spec §11) |
| `claims` | `answer.json` claims, through the existing `collect.py` drafting and `validate_answer.py` |
| `limitations[]` | derived and labelled by source: `findings.reason`, `noise_unmeasured`/`noise_reason`, `final_check.unmeasured_noise_findings`, `final_check.answer_valid=false` + `evidence_errors`, `halt.json`, ledger rows with `status: partial`, `baseline.json.noise.unmeasured`, plus coordinator-written limitations (for example BLIS representability), marked `source: coordinator` |
| `methods` | `baseline.json` (`command`, `git_sha`, `noise`), `plan.json` runs, `runs.jsonl` with `experiment: true` |

**Spotlights static.** `result.json` is a `SpotlightsManagerResult`.
Per-module status is `SUCCEEDED|DEGRADED|SKIPPED|FAILED` (`schemas/common.py:24`), and issues have the
form `{step, severity, message, recoverable}` (`schemas/common.py:45`).
`run_manifest.outputs.num_candidates` gives the candidate count (`costing/manifest.py:112`).

| Outcome | Rule |
|---|---|
| `failed` | exit ≠ 0 (exit 1 only for a `recoverable=False` issue, `cli.py:920-924`; exit 2 for a bad models file, `cli.py:874, 912`), or any `FAILED` module, or any `recoverable=False` issue, or `result.json`/`run_manifest.json` missing |
| `no_opportunity` | exit 0, `num_candidates == 0`, no `recoverable=False` issue, empty `manager_issues` |
| `candidates_found` (`partial` if any module is `DEGRADED` or `manager_issues` is non-empty) | exit 0, ≥1 candidate |

**Spotlights telemetry.** `SpotlightReport.issues` is **always `[]`** on this
path (`spotlight_report.py:307`), so the report never signals failure. The
outcome comes from the exit code (0/2/3/4, `signal_pipeline/cli.py:229-249`; 1 on an uncaught stage exception, `runner.py:924-942`),
the per-stage `status.json` `state`/`error`, the stdout JSON `issues`, and
whether `spotlight_report.json` exists. Zero candidates appear as
`03_candidates.json == []` with exit 0, which maps to `no_opportunity`.

`estimated_impact`, proposals, sort scores and costs are **never** turned into
claim values. Only candidate ids and verbatim code citations are evidence.

### Q5. Which material settings depend on environment variables or files outside the campaign?

| Engine | Outside input | Plan |
|---|---|---|
| Nous | `NOUS_CAMPAIGN_PARENT` (`work_dir_resolver.py:53`) | The runtime sets it in the prepared environment. Its value is material. |
| Nous | variables named in campaign `secrets:` | Presence only. Never values or hashes. |
| Nous | inherited `ANTHROPIC_*` / `CLAUDE_*` used by the Agent SDK: **not established** which ones | **Decision (D9, owner): not material.** Only the explicit `models.session` in the request counts. The values of the non-secret `ANTHROPIC_*` and `CLAUDE_*` variables are still **recorded** in the preparation, for the record, and `launch` **warns** (but does not refuse) if they differ from what was prepared. Variables whose names contain `KEY`, `TOKEN`, `SECRET`, `PASSWORD` or `AUTH` as a whole underscore-delimited segment (or as the whole name, case-insensitive), where the segment may have digits glued to the keyword inside it (`KEY2`, `2KEY`, `SECRET1`), are recorded as present or absent only. Accepted gap: a changed endpoint after approval isn't blocked, only warned about. Spec §7 lists "relevant environment configuration" as material, so the protocol doc has to state this narrowing. |
| Nous | Agent settings come from the **target worktree's** `.claude/` and CLAUDE.md (`setting_sources=["project","local"]`, `session.py:123`), not `~/.claude`. The session runs with `permission_mode=bypassPermissions` (`session.py:120`). | Covered by the target commit, because the worktree is built from HEAD. The `bypassPermissions` mode is stated in the protocol doc as a reason the runtime is not a sandbox. |
| Nous | Default model `claude-opus-4-6` when `models.session` is unset (`session.py:72`) | The adapter requires `models.session` explicitly. No implicit default. |
| Spotlights (both) | `SPOTLIGHTS_MODELS_FILE` (`model_config.py:25`), `SPOTLIGHTS_RATES_FILE`, `SPOTLIGHTS_EXTERNAL_RATES_FILE` (`costing/rates.py:24-25`), `SPOTLIGHTS_CLAUDE_BIN` (`_subprocess_util.py:59`). No other `SPOTLIGHTS_*` name appears under `src/` | The value of each is material, along with a digest of each file it points at. |
| Spotlights (both) | `~/.claude/settings.json` and `~/.codex/config.toml` decide the model when none is set (`model_config.py:4-6`; `cli.py:509-520`) | **Digest** of each file is material. Contents are never stored, because they may hold credentials. The adapter also requires an explicit model flag where the CLI supports one. |
| Spotlights telemetry | `~/.cache/spotlights-engine/projecttree/` cache (`s02_projecttree.py:71-72`) | The adapter passes `--no-projecttree-cache` (`signal_pipeline/cli.py:143`). That flag only skips the cache **read**; a fresh extraction is still written back (`s02_projecttree.py:158, 175`). The write is harmless to this run but is outside the run dir. |
| Spotlights telemetry | the `--telemetry-from` directory | Each input file's digest is material. |

### Q6. Which repository revision and dirty-worktree policy can the adapter enforce reproducibly?

- **Nous** builds its worktree from repo_path's **HEAD** (`git worktree add -b nous/<run_id> <wt> HEAD`, `loop.py:115-126`), so uncommitted changes are left out. It reuses an existing `nous/<run_id>` branch as-is, whatever that branch contains (`loop.py:119-122`). The worktree sits at `<repo>/.nous-experiments/<run_id>/`.
- **Spotlights (both paths)** runs agents in the **live working tree**, uncommitted changes included. It records `target.commit_sha` as a plain `git rev-parse HEAD` (`spotlights_manager/provenance.py:42-43`) and does not detect a dirty tree (no `porcelain`/dirty check under `spotlights_manager/`). The step-1 agent runs with `cwd=repo_path` (`modules_extractor/agent.py:168`). One exception is the signal-pipeline stage-02 cache key, which hashes porcelain status (`s02_projecttree.py:92-106`), but only for caching.

Policy, enforced at both prepare and launch:

1. The target must be clean: `git status --porcelain --untracked-files=normal -- . ':!investigations' ':!.nous-experiments'` is empty. `investigations/` is excluded because the study itself is written there, and in fork mode it is committed. That exclusion is decision D4.
2. HEAD at launch equals the prepared commit.
3. Nous only: the branch `nous/<run_id>` and the work dir `<runs>/<run_id>/state.json` must not exist for a fresh launch.
4. The material record stores both the commit and the clean-tree check result.

A dirty target is rejected and is never "approximated by HEAD" (spec §15).

### 1.7 R0 results (2026-11-09)

R0 read the engines only through `git show <pin>:<path>` and `git grep <pin>`.
It launched no engine and spent nothing. Spotlights paths below are under
`src/spotlights_engine/` (or `src/spotlights_engine/signal_pipeline/` for
`s0N_*`, `runner.py`, `_subprocess_util.py`). Nous paths are under
`orchestrator/`.

1. **Pinned facts.** Every Q1–Q6 and G1–G11 fact the design depends on was
   re-read at the pin, and its citation is now inline. The installed CLIs
   were built from the pins: `direct_url.json` in the uv tool envs records
   `e224cc1…` and `cccc290…`. Three corrections:
   - A halt raised in DESIGN or EXECUTE leaves no `halt` ledger row
     (`loop.py:387-389, 419-421`). The Q2 table now checks `halt.json`
     first and is ordered.
   - The Spotlights static path is mixed. Step 1 children run in new
     sessions; the other steps' children share the engine's group (Q3).
   - `--no-projecttree-cache` skips the cache read but still writes the
     cache (Q5).
2. **Approval card display.** Established for the Claude Code VS Code
   extension (`CLAUDE_CODE_ENTRYPOINT=claude-vscode`). The Bash tool has no
   TTY (re-checked). Its stdout reaches the agent, but the host does not
   reliably show it to the owner, and long tool output can be collapsed.
   The agent's own reply text is shown in full. So the card counts as shown
   only when the agent pastes it into its reply (Q1 step 2). Codex chat:
   **not established** (it couldn't be tested here). Whether a long reply
   gets truncated in either host: **not established**. Design change: Q1
   step 2 and R2.
3. **`spotlights-engine --dry-run`: no model call, no network.** The dry-run
   branch (`cli.py:864-902`) returns before `_configure_logging`,
   `_build_config` and `run_with_telemetry` (`cli.py:904-914`). It only
   flattens `--include` and resolves models (`cli.py:509-556`), which reads
   `SPOTLIGHTS_MODELS_FILE` or the bundled `models.yaml` and validates ids
   with a regex. No module under `src/` imports a network library; the
   only `urllib` use is `urllib.parse` in `report/render.py:19`. Two side
   effects:
   - It `mkdir`s `--artifacts-dir` and `--output-folder` **before** the
     dry-run branch (`cli.py:836-837`).
   - It does not check that `--include` modules exist.

   **Prepare may use it**, but only with throwaway `--artifacts-dir` and
   `--output-folder` paths in a temp dir that is deleted afterwards (R5).
4. **`ps -o lstart=`.**
   - macOS (run here): `Mon Nov  9 13:28:05 2026` plus trailing spaces.
     The output **changes with locale** (`fr_FR` gives
     `lun.  9 nov. 13:28:05 2026`) **and with TZ**, and it has 1-second
     resolution. A missing pid gives exit 1.
   - Linux (run here in a local `golang:1.27.2` Debian image, procps-ng
     4.0.4, `--network=none`): same format, also TZ-dependent, exit 1 for a
     missing pid. `/proc/<pid>/stat` field 22 (start time in clock ticks
     since boot) is also available.
   - `python:3.12-slim` (Debian trixie) has **no `ps` at all**. busybox `ps`
     (Alpine) rejects `lstart`.
   - GitHub `ubuntu-latest` was **not checked live** (not established). Its
     documented image includes procps.

   Design change: §4.3.
5. **Do `workload` extras reach the stage 03 prompt when `anomalies` is
   empty? Yes, verbatim. But the prompt gives them no job.**
   - The path: `_load_signals` validates with `extra="allow"`
     (`s01_signal_extraction.py:110-114`; `schemas.py:36`). `_run_single`
     writes the full `model_dump` (`runner.py:668-678`). Stage 03 reads it
     back (`runner.py:344-348`) and inserts
     `signals.model_dump_json(indent=2)` into `{signals_json}`
     (`s03_candidate_generation.py:91, 141`; `prompts/candidate_generation.md:16`).
   - No code skips stage 03 or filters fields when `anomalies == []`. A
     mirror of the pinned models under pydantic 2.9.2 kept nested
     `workload.metrics` intact.
   - However, the prompt's method is **anomaly-driven**: "For each anomaly
     that warrants further investigation" (`candidate_generation.md:53`).
     It says nothing about workload metrics, and `anomaly_refs` may be `[]`
     only for a candidate that is "not anomaly-rooted" (`:93`).
   - Stage 02 does not read Signals (`s02_projecttree.py:179-185`). Stage 04
     sees only one candidate (`s04_change_generation.py:76-81`).
   - **Verdict:** a reshape-only Signals file gives Spotlights the data but
     no instruction to use it. Whether stage 03 then returns candidates or
     `[]` depends on how the model behaves. That can't be established
     without a paid run, so it is **not established**. Expect `no_opportunity` or
     weakly grounded candidates. This needs an owner decision (R6a).
6. **SIGTERM to a process group vs `start_new_session=True` children.**
   This was run on a throwaway tree of small Python scripts in a `mktemp`
   dir, on macOS. The tree was supervisor → engine in its own session →
   one child in the same group and one child with `start_new_session=True`.
   - `os.killpg(engine_pgid, SIGTERM)` reached the engine and the
     same-group child. The new-session child **did not get the signal and
     kept running**.
   - Walking descendants first (`ps -ax -o pid=,ppid=,pgid=`, which also
     works on procps) and signalling each distinct pgid reached both
     children.
   - At the pin, signal-pipeline starts every `claude` child with
     `start_new_session=True` (`_subprocess_util.py:249`). Spotlights
     static does the same for step 1 only.
   - Design change: §4.4.

---

## 2. Findings that change existing docs or code

| # | Finding | Affects | Action |
|---|---|---|---|
| G1 | `nous-blis.md` documents `nous validate "$STUDY/campaign.yaml"`. At the pin, the syntax is `nous validate {baseline\|design\|execute\|report} --dir D`, run against a work dir (`cli.py:443-447`, phases from `hooks.py:62-67`). There is **no dry-run or plan mode** (no `dry` flag in `cli.py`). Confirmed in R0; `nous-blis.md:99` still has the old form. | R4, R8 | Fix the reference. The adapter's validate step checks the generated campaign itself, against the field facts already in `nous-blis.md`. |
| G2 | `signal-pipeline` writes `spotlight_report.json` to **`<artifacts-dir>/`** (`layout.root`, `spotlight_report.py:426`; `RunDirLayout(run_dir)` with `run_dir=--artifacts-dir`, `runner.py:786`, `signal_pipeline/cli.py:224`), not to `--output-folder` as the engine README says (`signal_pipeline/README.md:111, 124-126`). `collect_spotlights.find_result` looks only in `run_dir/{result.json, spotlight_report.json, report/spotlight_report.json}`, so it never finds the report under `artifacts/` (`collect_spotlights.py:82-87`). | R6 | Have the adapter pass the report path explicitly (add `--result PATH` to the collector), and add `artifacts/spotlight_report.json` to the search list. Fix `spotlights.md`. |
| G3 | `--max-cost` is advisory (Q3 citations), but `spotlights.md:38` and the plan template's cost table read as if it were a cap. | R5, R8 | Label it advisory everywhere. The runtime records it under `limits.advisory`. |
| G4 | `signal-pipeline` with **no** `--telemetry-from` runs on a synthetic stub workload (`s01_signal_extraction.py:167-197`, `workload_id="stub-workload"` at line 172) and exits 0. | R6 | The adapter requires `--telemetry-from`. A result whose `workload_id == "stub-workload"` is rejected as `failed`. |
| G5 | The CLI default is `--to-stage 04` (`signal_pipeline/cli.py:204`), but stage 05 can also run through `--to-stage 05`, `--only-stage 05` (`cli.py:200-201`) and through the Python API `run_pipeline(stages=None)`, which defaults to stages 01–05 (`runner.py:116, 785`). `--inject 05…` marks stage 05 done (`runner.py:499, 589`). | R6 | The adapter builds argv itself: `--to-stage 04` always. It never accepts `--from-stage`, `--only-stage` or `--inject`, and never imports `run_pipeline`. Tests assert this. |
| G6 | `nous stop` takes effect only after the current iteration's EXECUTE phase, and REPORT then still runs (a full LLM turn) (Q3 citations; the CLI's own message says so, `cli.py:234-238`). `nous-blis.md:139` says "next phase boundary". | R3, R4, R8 | Fix the wording. Cancellation is reported as `cancel_requested` until a terminal state is confirmed. |
| G7 | Neither engine writes a pid or lock file (no `pid`/`lock`/`flock` match under either engine's source at the pin). | R3 | The runtime supervisor records process identity itself (§4.3). |
| G8 | Spotlights static exits 1 only on non-recoverable issues (`cli.py:920-924`). `DEGRADED` modules exit 0. It also exits 2 on a bad models file (`cli.py:874, 912`). | R5 | Mapped to `candidates_found` + `partial` (Q4). |
| G9 | The Spotlights static `run_id` is a hash of its input fingerprint (`spotlights_manager/orchestrator.py:127-144`). The fingerprint has repo path, context and flags but no commit or timestamp (`persistence.py:319-349`). An existing artifacts dir is resumed by default. | R5 | Each invocation gets a fresh artifacts dir that must not already exist. A resume is a separately prepared invocation that reuses the prior artifacts dir and is linked to that attempt. |
| G10 | `signal-pipeline` stage 01 accepts a Signals JSON (`01_signals.json`/`signals.json`/`signal.json`, `s01_signal_extraction.py:45`) or a directory containing `traces.jsonl` (`:50, 203-211`). It does **not** accept a metrics-only directory. The `Signals` schema itself needs **only** `workload.workload_id`: `traces` and `anomalies` default to `[]` (`signal_pipeline/schemas.py:35-62`). `workload` allows extra fields; top-level `Signals` forbids them (`schemas.py:36, 58`). So **Prometheus metrics alone are valid input once they are reshaped into Signals**, and traces are optional. | R6a, R6, R9 | **Decision (D6, owner):** the skill reshapes Prometheus captures into a Signals JSON (new phase R6a), **with no anomaly logic**. That partly revises v2-plan F1: the skill now writes the Signals file but still does no anomaly detection. The telemetry-driven adapter then accepts the skill's Signals output, an existing Signals JSON, or a directory containing `traces.jsonl`. |
| G11 | `pyproject.toml:3` says `0.1.0` at tag `v0.1.1`. The installed tools' `direct_url.json` records the commit: `cccc290f…` (Spotlights, `rev=v0.1.1`) and `e224cc1…` (Nous, `nous-v2-fixes`). | R5 | Version checks compare the git sha that doctor already reports, not the package version string. |

---

## 3. Architecture

### 3.1 Layout

```
skills/investigation/
  scripts/
    runtime.py                  # CLI entry point (thin; argparse -> ains_runtime.cli)
    ains_runtime/
      __init__.py               # RUNTIME_VERSION, SCHEMA_VERSIONS
      canonical.py              # canonical JSON, sha256 digests, material fingerprints
      contracts.py              # message dataclasses + stdlib validators (no jsonschema dep)
      store.py                  # SQLite store: schema version, transactions, migrations
      artifacts.py              # artifact refs {kind, location, digest, provisional}
      gitstate.py               # commit / clean-tree checks (reuses _config.git)
      envpolicy.py              # env allowlist, secret detection, material env record
      approval.py               # chat-sourced approval: short-code check, digest binding, consumption
      limits.py                 # required engine bounds, advisory limits, enforcement labels
      supervisor.py             # detached supervisor: spawn, record identity, wait, exit code
      procid.py                 # process identity (pid + start time + argv digest)
      recovery.py               # reconcile reserved/spawned records after a crash
      results.py                # result record: execution status / outcome / validation
      export.py                 # reviewable JSON export into the study
      adapters/
        base.py                 # Adapter protocol: validate, prepare, execute, collect, cancel
        nous.py
        spotlights_static.py
        spotlights_telemetry.py
        telemetry_capture.py    # Milestone B
    runtime.py
  references/
    runtime-protocol.md         # CLI, lifecycle, approval, limits, trust boundary
    runtime-schemas/            # versioned JSON Schema docs + examples (documentation; validation is in contracts.py)
      envelope.v1.json  experiment-request.v1.json  receipt.v1.json  progress.v1.json
      clarification.v1.json  cancel.v1.json  experiment-result.v1.json  examples/
```

Constraints carried over from the v2 plan:

- Python 3.10+, the standard library and PyYAML only. PyYAML is needed only to render Nous `campaign.yaml`. `sqlite3` is in the standard library.
- Every import resolves relative to `Path(__file__)`. There are no absolute paths, which the `no-hardcoded-paths` CI job already checks.
- `doctor.py` and `validate_answer.py` stay stdlib-only and do not import `ains_runtime`.

### 3.2 State locations

| What | Where | Authority |
|---|---|---|
| Runtime DB | `$RUNS/<repo>/<slug>/.runtime/runtime.sqlite3` (WAL mode) | execution bookkeeping |
| Prepared immutable inputs (for example, a rendered `campaign.yaml`) | `$RUNS/<repo>/<slug>/.runtime/prepared/<prep-id>/` (made read-only after prepare) | digests recorded in the DB |
| Supervisor and engine logs | `$RUNS/<repo>/<slug>/.runtime/logs/<inv-id>.{supervisor,engine}.log` | provisional until the invocation reaches a terminal state |
| Native engine output | `$RUNS/<repo>/<slug>/<run_id>/` (Nous), `$RUNS/<repo>/<slug>/spotlights-<id>/` (Spotlights) | capability evidence |
| Exported records | `$STUDY/runtime/{requests,preparations,approvals,results}/<id>.json` | review copies only. Copying them grants nothing (spec §6) |

The existing `fork`/`untracked` tracking modes and `--commit-guard` are
unchanged. `$STUDY/runtime/` is ordinary study content.

### 3.3 Store schema (v1)

The tables are `meta(schema_version, runtime_version)`, `messages`,
`work`, `preparations`, `approvals`, `invocations` and `events`:

- **`messages(message_id PK, payload_digest, type, work_id, received_at)`.** An identical id with an identical digest returns the stored receipt. An identical id with a different digest is rejected as a `conflict`.
- **`work(work_id PK, revision, state, ...)`.**
- **`preparations(prep_id PK, work_id, material_digest, record_json, created_at)`.**
- **`approvals(approval_id PK, prep_id UNIQUE, material_digest, source, owner_quote, approved_at, consumed_at NULL)`.**
- **`invocations(inv_id PK, prep_id UNIQUE, material_digest, repeat_of NULL, state, supervisor_pid, supervisor_start, engine_pid, engine_start, argv_digest, native_run_id, exit_code, ...)`.**
  - A partial unique index on `material_digest WHERE repeat_of IS NULL AND state NOT IN ('rejected')` stops two equivalent invocations from running, even when their message ids differ (spec §6, §9).
- **`events`** is append-only: one row per lifecycle transition.

Every state-changing operation runs in `BEGIN IMMEDIATE`. Reservation,
consuming the approval and the duplicate check happen in **one**
transaction, before any process is created (spec §8).

### 3.4 CLI

| Command | Who runs it | Effect |
|---|---|---|
| `runtime.py submit <msg.json>` | agent | Validates the envelope and type. Records the message and returns the receipt. Idempotent. |
| `runtime.py prepare <work-id>` | agent | Adapter validate + prepare. Writes immutable inputs and prints the summary, the digest and the exact `approve` command. |
| `runtime.py show <prep-id\|inv-id\|work-id>` | anyone | Read-only view. `--json` for the agent. |
| `runtime.py approve <prep-id> --code <short> --source chat --quote "<reply>"` | agent, **only after the owner's go in chat** | Checks the short code against the digest, then records a single-use approval bound to that digest with its source and the owner's quoted reply (D1). |
| `runtime.py launch <prep-id>` | agent | Revalidates the inputs, versions, commit and clean tree, then reserves atomically, consumes the approval and starts the supervisor. Returns at once. |
| `runtime.py status <work-id\|inv-id>` | agent | Reconciles with native state (adapter) and prints the lifecycle state. |
| `runtime.py cancel <inv-id>` | agent (owner asked) | Records the cancel request and calls the adapter's stop mechanism. Reports `cancel_requested` until a terminal state is confirmed. |
| `runtime.py collect <inv-id>` | agent | Read-only adapter collection, using the existing collectors. Can be retried. Never reruns the experiment. |
| `runtime.py result <inv-id>` | agent | Builds and exports the `experiment-result.v1` record. |
| `runtime.py recover [--study]` | agent | Reconciles invocations stuck in `reserved` or `spawned`. Can mark `recovery_required`. |
| `runtime.py reconcile <inv-id> --mark-not-launched --quote "<reply>"` | agent, **only after the owner confirms in chat** | Resolves a `recovery_required` invocation as never launched. The approval stays consumed, so any new attempt needs a new preparation and approval. |
| `runtime.py export` | agent | Rewrites `$STUDY/runtime/` from the DB. |

---

## 4. Design details that the spec leaves open

### 4.1 Messages (spec §6)

The envelope fields are `schema` (for example `ains.experiment-request/v1`),
`message_id`, `work_id`, `revision`, `reply_to?`, `target{repo_name,
commit}` and `artifacts[]`. The supported types are `experiment-request`,
`receipt`, `progress`, `clarification` (question and answer),
`cancel` and `experiment-result`.

- Unknown `schema` versions are rejected.
- Missing required fields are rejected. Fields are never filled in by inference.
- An artifact ref is `{kind, location, digest?, provisional}`. A digest is required unless `provisional: true`.
- Absolute local paths are allowed only in `location`, never as an identity.
- `contracts.py` rejects payloads containing any value from an environment variable whose name matches the secret pattern. A test plants a fake token in the environment and checks it is refused.

The `experiment-request` body has these fields:

- `capability` ∈ `nous | spotlights-static | spotlights-telemetry | telemetry-capture`.
- `question`, `task_type`, `objective`, `baseline?`.
- `hypothesis?`: required for a supported/not-supported classification.
- `locks{}`, `limits{}`.
- `next_step{interpretation_of_prior, remaining_uncertainty, expected_information_gain, rationale}`. This is required whenever the study already has a result.
- `repeat_of?` + `repeat_reason?`.
- A capability-specific `semantics{}` block. For Nous it holds target_system description, model, experiment_commands, task_limits and run_id. The native YAML is **not** the public contract (spec §10).

### 4.2 Material fingerprint (spec §7)

`material_digest` is `sha256(canonical_json(material))`. `material` is the
output of the adapter's **explicit allowlist** of fields. Each adapter has a
`MATERIAL_FIELDS` table plus a test that changes each field one at a time and
checks the digest changes.

For each adapter, fields that may vary without changing the digest are listed
with a reason, each with its own test:

- **Nous:** `run_id` → work-dir name and `nous/<run_id>` branch. The test
  shows that renders differing only in run_id give byte-identical campaign
  YAML apart from `run_id:`.
- **Spotlights:** `--output-folder` and `--artifacts-dir` paths.

Everything else is material, including the adapter version, engine sha,
runtime version, env record, model-config file digests, target commit and the
clean-tree result.

### 4.3 Execution and process identity (spec §8)

1. `launch` runs a single transaction that checks the approval and duplicates, consumes the approval, and inserts the invocation as `reserved`.
2. It then spawns `runtime.py _supervise <inv-id>`, detached with `start_new_session=True`, and returns.
3. The supervisor records its identity as `supervisor_pid` + `supervisor_start`, with state `spawning`.
4. It spawns the engine from the prepared argv. The argv is a list and no shell is used. The environment is built only from the allowlisted record plus secrets passed through by name.
5. It records `engine_pid`, `engine_start` and `native_run_id`, sets state `running`, waits, and records `exit_code` and the end time.
6. It writes nothing into engine directories. It does **not** collect automatically and does **not** chain to another capability.

Process identity is the triple pid + start time + argv digest. A pid alone
is never trusted (spec §8). R0 found that `ps -o lstart=` output changes with
locale and TZ, has 1-second resolution, and that `ps` is missing from slim
Debian images (§1.7 item 4). So `procid.py`:

- on Linux, reads start time from `/proc/<pid>/stat` field 22 (clock ticks
  since boot, no `ps` needed). Elsewhere it runs `ps -o lstart= -p <pid>`
  with `LC_ALL=C` and `TZ=UTC` forced in the child environment, and strips
  whitespace.
- stores the value as an opaque string tagged with its method
  (`proc-stat` or `ps-lstart`). It compares only like with like, and never
  parses the value as a date.
- treats "no way to read start time" as a launch-blocking error (doctor
  reports it), not as "pid only".
- uses the argv digest to tell apart two processes that started in the same
  second with a reused pid.

Recovery rules (`recover`, and run implicitly by `status`):

| Recorded state | Evidence | Result |
|---|---|---|
| `reserved`, no supervisor identity | no supervisor or engine process matches the identity or argv digest, **and** no native artifacts (Nous: work dir/`state.json`; Spotlights: artifacts dir) | `recovery_required` (launch may or may not have happened). The owner resolves it with `reconcile`. |
| `reserved`/`spawning`, native artifacts exist | — | `recovery_required` with the evidence attached. Never relaunched. |
| `running`, engine identity matches a live process | — | stays `running` |
| `running`, engine gone, supervisor gone | adapter classification from native evidence (Q2/Q4) | terminal state, or `failed` + `exit_code: unknown` |

Exactly-once launch across the reserve→spawn gap is **not** promised, as
spec §8 requires.

### 4.4 Cancellation (spec §8)

| Adapter | Mechanism | Confirmed when |
|---|---|---|
| Nous | Default: `nous stop <work_dir> --reason <cancel id>`, the cooperative STOP file. Optional `--interrupt`: SIGINT to the engine pid (exit 130, resumable). SIGTERM is never used, because Nous has no handler. | `final_check.json` with `stop_sentinel`, or exit 130 recorded by the supervisor |
| Spotlights static and telemetry | No native stop. The supervisor starts the engine in its own session, so the engine's pgid is its pid and the supervisor is outside that group. To cancel, it snapshots the engine's descendants (`ps -ax -o pid=,ppid=,pgid=`) and sends SIGTERM to **every distinct pgid** among the engine and its descendants. After a grace period it re-snapshots and sends SIGKILL to the same groups. Signalling only the engine's group is not enough: R0 showed that children started with `start_new_session=True` don't get the signal and keep running. Signal-pipeline starts every `claude` child that way, and Spotlights static does for step 1 (§1.7 item 6). This is **best effort**. A descendant reparented to init before the snapshot is missed, so the cancel result lists any process still alive in the recorded groups. **Documented as unsupported interruption:** final reports are not written and stages stay `in_progress`. | supervisor records the engine's exit, and the re-snapshot finds no live process in the recorded groups |

If the engine reaches a terminal state before cancellation takes effect, the
runtime reports the real terminal state. Partial artifacts are always kept.

### 4.5 Limits (spec §9)

**Decision (D8, owner): no invocation-count caps.** The guards against
runaway launches are: one approval per invocation, no automatic retry or
chaining, and the duplicate check. **Spec §9 ("enforce configured invocation
counts... bounded study invocation count") needs that amendment.** The DB
still counts invocations per study and per capability, and the count appears
on every approval card so the owner can see how many runs they have approved.

Runtime-enforced bounds that remain:

- Every Nous request must carry a `max_iterations` value. Nous enforces it.
- One resume per invocation without a new preparation. Every resume is a new
  preparation and needs a new approval anyway.

**Decision (D7, owner): time limits are advisory.** The runtime reports
elapsed time against any stated budget and never kills a run because of time.

Every limit in a result record carries an `enforced_by` label:
`runtime | engine | advisory`. Spend appears as
`{reported_usd, completeness: complete|partial|unknown, source}`.

Retries, resumes and repeats:

- A resume or repeat needs `repeat_of`, a `repeat_reason`, a new preparation
  and a new approval.
- A `not_supported` or `inconclusive` outcome is rejected as a
  `repeat_reason`. This is checked against the prior result's outcome.

### 4.6 Results (spec §11)

`experiment-result.v1` has these parts:

- `execution{state, native_stop_reason, exit_code, started_at, ended_at, diagnostics[artifact refs]}`
- `outcome{classification: supported|not_supported|inconclusive|other|unavailable, hypothesis_ref, explanation, partial: bool}`
- `findings{answer_ref?: artifact ref to a validated answer.json, claims_from: "answer-contract/v1"}`
- `validation{state: not_run|passed|failed, validator: "validate_answer.py", report_ref}`
- `methods`, `limitations[]` (each with a `source`), `limits[]`, `spend`, and `versions{runtime, adapter, engine_sha}`

Failure results never carry findings. A collection or validation failure sets
`validation.state` and leaves `execution` unchanged.

`answer.json` and `validate_answer.py` are **unchanged** in Milestone A.

### 4.7 Coordinator changes (spec §12)

- **`plan.md` becomes a provisional method outline.** Approving it never grants a launch.
- **A new `## Step log` section in `plan.md`.** After each result it records interpretation, remaining uncertainty, the proposed next step or stopping reason, expected information gain, and the limits that apply. The `next_step` block in the next request is copied from this entry. The runtime checks that the block is present and non-empty. Whether the step is worthwhile stays a judgement for the coordinator and the owner (spec §4).
- **SKILL.md steps 2, 4 and 5 are rewritten.** Migrated methods (Nous and both Spotlights paths in Milestone A) go through `runtime.py submit → prepare → [owner approve] → launch → status → collect → result`. SKILL.md forbids calling `nous run|resume`, `spotlights-engine` or `signal-pipeline` directly. Telemetry capture is labelled **"legacy gate, not runtime-enforced"** until B1.
- **A new hard gate:** the agent runs `approve` or `reconcile` only right after the owner's explicit go in chat for that card. It quotes the owner's reply and the short code, and never infers or carries over a go.
- SKILL.md stays under 500 lines, as `check_skill_spec.py` already checks. The detail goes into `references/runtime-protocol.md`.
- **Frontmatter:** `metadata.runtime-version`. `doctor.py` reports the runtime version, the DB schema version it can read, and a `runtime` method-availability entry (it reads `RUNTIME_VERSION` from `ains_runtime/__init__.py` as text, so it stays stdlib-only).

---

## 5. Phase plan — Milestone A

Dependencies:

```
R0 → R1 → R2 → R3 → (R4 ∥ R5 ∥ R6a→R6) → R7 → R8 → R9
```

R4, R5 and R6 touch separate adapter files. R7 needs at least one adapter but
is finished only when all three are done. **Milestone A is complete only at
R9**, with all three paths working (spec §14).

### R0 — Verification spikes (costs nothing, no engine launches)

- Re-confirm the load-bearing pinned-source facts in §1 and §2 (G1–G10), with citations. Record them in this document's §1.
- Confirm the approval card prints fully in Claude Code and Codex chat output (not truncated or collapsed).
- Check whether `spotlights-engine --dry-run` makes any model call or network request, by reading the source. Use it in prepare only if it makes none.
- Check `ps -o lstart=` on macOS and on a Linux CI image.
- Read `signal_pipeline` stages 02–04 at the pin to check whether `workload` extras reach the stage 03 prompt when `anomalies` is empty (R6a).
- On a throwaway fake process tree, check whether SIGTERM to a process group reaches children started with `start_new_session=True`. This sets the Spotlights cancellation wording.

**Status: done (2026-11-09).** Results are in §1.7. The design changes are
folded into Q1, Q2, §4.3, §4.4, R2, R3, R5 and R6a. The owner answered
R0-1 with (b), hold R6a/R6 (§7, D10).

**Done when:** every row in §1 is either cited or marked not established, and
any design change those findings force is folded into this plan. The owner decisions are recorded in §7.

### R1 — Contracts, canonicalization, store

- `canonical.py`, `contracts.py`, `artifacts.py`, `store.py` (schema v1 + version check + migration hook), `runtime.py submit|show|export`.
- Schema docs and examples under `references/runtime-schemas/`.
- **Tests:**
  - every message type, valid and malformed
  - unsupported schema version is rejected
  - same id + same payload returns the existing receipt
  - same id + different payload is a conflict
  - canonical JSON is stable across key order and unicode
  - the secret-value planting test
  - copying the study without the DB grants nothing (a fresh DB rejects launching an exported preparation)

### R2 — Preparation and approval

- `gitstate.py`, `envpolicy.py`, `approval.py`, the `Adapter` protocol, `prepare` and `approve`, and a `fake` adapter (test-only, under `tests/`) that drives the engine-independent tests.
- The approval card is plain Markdown of bounded size: material summary, limits, defaults, the counts (D8), the full digest and the short code, with no raw file contents. `prepare` prints it and also writes `prepared/<prep-id>/card.md`. The skill's instruction is to paste the card into the agent's reply, not to rely on tool output being visible (R0, §1.7 item 2).
- **Tests:**
  - prepare runs before approve (`launch` with no approval is refused)
  - `approve` refuses a missing or wrong short code, and a missing `--quote`
  - an approval binds to one digest and can be used once
  - a material change after approval leaves the old approval unusable
  - dirty target / HEAD drift / version drift between prepare and launch is rejected
  - secrets are never in records or logs
  - the card stays under a fixed size bound, and the printed card is byte-identical to `card.md`

**Status: done (2026-11-09).** Deviations from the text above:
- One extra module, `ains_runtime/preparation.py`: `prepare()` and
  `revalidate()`, the pre-launch check R3's `launch` calls before
  `reserve_invocation(..., expected_digest=...)`. `adapters/__init__.py` is the registry.
- `approve` also requires the `--quote` to contain the short code (D1 says the
  owner quotes it), and `--source` must be `chat`.
- The fake adapter (`tests/_runtime_fake_adapter.py`, CLI wrapper
  `tests/_runtime_fake_cli.py`) registers under a real capability name
  (`nous`), because contracts accept only the four listed capabilities. The
  shipped CLI has no adapter-loading hook.
- Non-material values (prep dir, run id, output paths) are replaced by
  `{name}` placeholders inside the material copy, so equivalent requests
  get the same digest and the duplicate index sees them.

### R3 — Execution control, limits, recovery, cancellation

- `supervisor.py`, `procid.py`, `limits.py` (labels and required bounds only), `recovery.py`, `launch|status|cancel|recover|reconcile`.
- Fake engines in `tests/fixtures/runtime/fake_engines/`: succeed, fail, sleep-until-signalled, crash after writing partial artifacts, ignore SIGTERM.
- **Tests:**
  - concurrent equivalent submissions (multiprocessing, different message ids) start exactly one engine
  - the duplicate check and approval consumption happen in the same transaction as the reservation
  - crash injection between reserve and spawn (an env hook makes the supervisor `os._exit` at a named point) ends in `recovery_required` with no relaunch
  - crash after spawn reconciles to the real state
  - cancel before and after the engine finishes
  - a cancel receipt does not mean the engine stopped
  - a pid reused by a different process is not mistaken for the engine
  - start-time capture gives the same string under a different `LC_ALL`/`TZ` in the caller's environment, and a missing `ps` or `/proc` blocks launch
  - cancel reaches a fake engine's child that was started with `start_new_session=True` (the R0 tree, as a fixture)

**Status: done (2026-11-09).** Tests: `tests/test_runtime_exec.py` plus `LaunchCopiedStudy` in
`tests/test_runtime_cli.py`. They were also run on Linux (`/proc` path, `--network=none`, non-root). Deviations:
- **Stored states are finer than the lifecycle.** The stored states are `reserved | spawning | running | exited | recovery_required | completed | failed | cancelled | not_launched | rejected`.
  `exited` means the supervisor recorded the exit and classification is pending. `not_launched` is what `reconcile` sets. Both are reported as lifecycle states
  (`exited`→`running`, `not_launched`→`rejected`). Any in-flight state with a recorded cancel request is reported as `cancel_requested`.
  `not_launched` frees the duplicate index, so equivalent material can launch again, but only through a new preparation and a new approval.
- **Classification runs in `status`/`recover`, not in the supervisor.** It goes through a new adapter hook,
  `classify(invocation, record, ctx) -> NativeState`. A second hook, `evidence(...)`, supplies recovery evidence and has a default implementation.
  The supervisor loads no adapter.
- **The spawn spec is stored with the reservation** (`invocations.launch_json`: argv, cwd, env_set, env names to pass, log paths). The launcher's pid and start time are stored too.
  So `reserved` with a live launcher counts as "launch in progress", not a crash.
- **Engine environment.** The engine gets only these variables: a base set (`PATH`, `HOME`, locale, proxy and CA variables), the preparation's material, file and secret variable names,
  the `ANTHROPIC_*`/`CLAUDE_*` variables, and the adapter's `env_set`.
- **Crash hook.** It fires only when both `AINS_RUNTIME_TEST_ONLY_CRASH_AT=<point>` and `AINS_RUNTIME_TEST_ONLY_CRASH_ARM=kill-this-runtime-process-for-a-test` are set.
  The engine never inherits them, and `launch` warns and records an event when they are present.
- **`reconcile --mark-not-launched`** refuses when a live process matches the engine argv or the supervisor argv.
- **Not done here.** doctor does not yet report the start-time method; that moves to R8 with the rest of the doctor work.
  Process identity is `ps -ww -o stat=,lstart=,command=` with `LC_ALL=C` and `TZ=UTC`, or `/proc/<pid>/{stat,cmdline}` plus the boot id. Zombies count as gone.
- **Found in R2's templating.** A non-material value is replaced in *every* string, so a short `run_id` (for example `r1`) that also occurs inside a random path changes the digest.
  The duplicate check then fails to see the two runs as equivalent. Tests use distinctive run ids. R4/R5 should generate long, unique run ids, or the templating should match whole argv elements only.

### R4 — Nous adapter

- `validate`:
  - doctor's `nous-blis` availability check, plus engine sha == pin
  - required semantics are present: question, task_type, objective, `models.session`, `max_iterations`, target_system description
  - BLIS representability and apparatus locks (binary sha256, catalog commit) are checked against the apparatus, using the pre-launch checks from `nous-blis.md`
  - work dir, branch and running-process collision checks
- `prepare`: renders `campaign.yaml` from the request (no `prompts.*`/`metadata`), writes it read-only, and builds argv `["nous","run",<campaign>,"--run-id",<id>]` + env `NOUS_CAMPAIGN_PARENT`.
- `collect`: wraps `collect.py`, extracting a reusable function rather than copying it. Adds bundle/findings classification and limitation derivation (Q4).
- `cancel`: as in §4.4.
- **Fixtures:** extend the existing `workdir-*` fixtures with halt, stop_sentinel, max_iterations, and `PARTIALLY_CONFIRMED`/`REFUTED` findings.
- **Tests:**
  - each Q2 table row
  - material mutation (each MATERIAL_FIELD) is rejected after approval
  - unsupported (non-BLIS) questions are rejected with a reason
  - the rendered YAML never contains ignored keys

**Status: done (2026-11-09).** Code: `ains_runtime/adapters/nous.py` (registered in
`adapters/__init__.py`), Q4 functions added to `collect.py` (`normalize`, `hypotheses`,
`limitations`, `methods`, `spend`; the CLI's bundle is unchanged). Tests:
`tests/test_runtime_nous.py`; fixtures `workdir-{halt,halt-design,stop-sentinel,max-iterations}`.
Deviations:
- **Request semantics for Nous** (no schema existed): `target_system` (description, optional
  name), `models.session`, `objective{metric, direction}` (the body's `objective` is free text,
  but Nous needs `{metric, direction}`), and `blis{knobs[], catalog?, limitations[]?}`. Optional:
  `experiment_commands`, `task_limits`, `locked_workload`, `min_improvement`,
  `stall_iterations`, `effort`, `session_scope`, `secrets`. `run_id` is the only non-material
  key. `body.locks` is rendered verbatim as `locked_parameters` and must carry `blis_binary`,
  `blis_binary_sha256` and `catalog_commit`. The R1 example request (`target_system` string,
  no `objective`/`blis`, lock names) does not pass this adapter's validate; R8 updates it.
- **BLIS representability** is checked two ways: each `blis.knobs` flag must match the
  representable set in `method-plan.md` (`--pd-*`, the three KV-offload flags, the two
  block-size flags), and the question/objective/hypothesis text is scanned for the
  not-representable topics. The text scan is a guard, not proof; the knob list is the contract.
- **Apparatus checks** run in validate, so also at launch (revalidate): binary sha256 and catalog
  HEAD against the locks; the measured values are in the material. `df` is shown on the card
  only. G1 holds: no `nous validate` call (it needs a work dir).
- **Generated run id** is `<work-id>-<prep-id>` when `semantics.run_id` is absent; a given
  run id must be 8–100 chars. Rendered `target_system.description` gets the request's
  objective, baseline and hypothesis appended. `human_gates` and `rerun_repro` are never
  rendered (the first isn't read by any code at the pin).
- **Collisions** also check the legacy `<repo>/.nous/<run_id>/state.json` and the
  `.nous-experiments/<run_id>` worktree. Any live `nous run|resume` or process running the
  locked binary blocks (one campaign at a time).
- **Q2 extras.** A run with `state.json`, no `final_check.json`, exit 130 and a recorded cancel
  is `cancelled` (the `--interrupt` path). No `state.json` at all is `failed` (the engine died
  before creating its work dir). A `final_check.answer_valid` that is not true adds a warning.
- **`collect()` returns** `{capability, state: collected|unavailable, reason?, work_dir,
  native_run_id, native_state, partial, outcome{classification: unavailable, reason, partial},
  hypotheses[{id, iteration, statement, statement_ref, native_status, classification,
  error_type, findings_ref, partial_note?, partial}], claims, answer_ref, final_check, usable,
  limitations[{source, text, location}], methods{baseline, plans, experiment_runs},
  spend{reported_usd, completeness: partial|unknown, source, note}, bundle}`. Limitation
  sources are `nous:<artifact field>`, `coordinator` (from `semantics.blis.limitations`) and
  `runtime` (partial findings). Ledger `prereg_violation` rows are also listed as limitations.
- Tests never need Nous installed: the adapter takes injectable `probe`, `list_processes` and
  `run_cmd` hooks. The default probe reuses `doctor.methods()`/`engine_info()` (offline).

### R5 — Spotlights static adapter

- `validate`:
  - engine sha == pin
  - `--include` modules exist at the commit
  - an objective is present
  - the artifacts dir is outside the repo (the engine also refuses this) and does not already exist
- `prepare`: argv with an explicit model flag, `--max-cost` recorded as advisory, and fresh `--output-folder`/`--artifacts-dir`. Prepare runs `spotlights-engine --dry-run` with the same flags, but with `--artifacts-dir`/`--output-folder` pointed at a temp dir that is deleted afterwards (the dry run `mkdir`s both, and the real artifacts dir must not exist yet, G9). It records the resolved models from the dry run's output. R0 showed the dry run makes no model or network call (§1.7 item 3). It does not check `--include`, so validate still checks module existence itself. The objective-setting and sort-candidates skills stay coordinator-side, in-session steps. Sort runs after collection and is not a governed launch, because it is not an expensive engine invocation. Decision D5.
- `collect`: wraps `collect_spotlights.py` and applies the Q4 outcome rules.
- **Tests:** `no_opportunity` vs `failed` vs `partial` fixtures, and estimated impact never appears in any claim value. The second check reuses the collector's existing `estimate_values` guard.

**Status: done (2026-11-09).** The adapter is `adapters/spotlights_static.py`. Tests are in
`tests/test_runtime_spotlights_static.py`, with fixtures in `tests/fixtures/runtime/spotlights_static/`.
They inject the engine probe and the `--dry-run` runner, so they never run `spotlights-engine` and pass
whether or not it is installed. One end-to-end launch uses a fake engine script. Deviations:
- **Request shape.** `semantics` = `include` (required, slash-form), `models{claude, codex}` (both
  required; both `--claude-model` and `--codex-model` are always passed, so models.yaml and
  `~/.claude`/`~/.codex` never decide the model), optional `hints`, `repo_url` and `secrets` (env names
  passed through by presence). All of them are material, and there are no non-material semantics keys.
  `--max-cost` comes from `limits.max_cost_usd`.
- **Output paths.** `$RUNS/<repo>/<slug>/spotlights-<prep-id>/` with `artifacts/` inside, both
  non-material placeholders. The prep id is long and random, so whole-token templating can't collide.
  `--no-resume` is always passed, so the engine itself refuses an existing run dir. Resume is not
  supported yet (it would be a separate preparation reusing the prior dir, G9).
- **`--include` check is approximate.** Module names come from the step-1 LLM extractor, so they can't
  be known before a run. Validate accepts a name only if it matches a directory or file stem at the
  request commit, using the engine's segment normalization and allowing any source-root prefix.
  Otherwise it rejects it, which beats the engine's warn-and-skip (`filters.py:56-112`).
- **Material env (Q5).** The four `SPOTLIGHTS_*` file vars (value + file digest), `CODEX_HOME`, and the
  digests of `~/.claude/settings.json` and `$CODEX_HOME/config.toml`. The resolved models the dry run
  prints must equal the requested ones, and they are part of the material.
- **Engine sha (G11).** Read through doctor's `engine_info` (direct_url.json, offline, no subprocess).
  Only a PATH install is launchable; a configured checkout is refused.
- **Classification (Q4/G8).** Besides the table: a run where no module was SUCCEEDED or DEGRADED is
  `failed`. Zero candidates together with DEGRADED modules or manager issues is `failed`
  (`unclassified_native_state`), never `no_opportunity`. A cancel with no `result.json` is
  `cancelled` (partial). `completed` carries `native_stop_reason` ∈ {`candidates_found`,
  `no_opportunity`}. A manifest commit that differs from the prepared commit is a warning.
- **`collect()` return shape** (for R7): `{capability, adapter_version, outcome ∈ candidates_found |
  no_opportunity | failed | cancelled, partial, reason, native (NativeState dict), exit_code,
  n_candidates, module_status, issues[{source, module, step, severity, message, recoverable}],
  evidence[{kind, location}], limitations[{source, text}], spend{reported_usd, completeness, source},
  versions{engine_sha, manifest_spotlights_commit}, collection{state ∈ not_run | collected | failed,
  path, error}, claims_draft[], candidates_top[], estimate_guard{checked, violations}, notes[]}`.
  Failed and cancelled runs are not collected. The study dir defaults to
  `<target>/investigations/<slug>`, and `ctx.study` overrides it if set.
- **Collector.** `collect_spotlights.collect_run(study, run_dir, *, top, repo, sha)` is the extracted
  reusable function. The CLI and its tests are unchanged.
- One R2 test used `spotlights-static` as its example of a capability with no adapter; it now uses
  `spotlights-telemetry`.

### R6a — Prometheus → Signals conversion (D6)

- New `scripts/telemetry_to_signals.py`: reads one or more existing frozen
  captures (`telemetry/<id>/`, from `capture_prom.py`) and writes
  `telemetry/<id>/signals/01_signals.json`.
  - It is stdlib-only, deterministic and read-only over the capture.
  - It does no network access. Fetching from Prometheus stays with
    `capture_prom.py`, which already takes one or more absolute time windows.
- **Reshape only, no anomaly logic:**
  - `workload`: `workload_id` = capture id, a description built from the
    capture's cluster and window, and extra fields carrying the window, step,
    query set digest and `source: "prometheus-capture"`.
  - `traces`: `[]`.
  - `anomalies`: `[]`.
  - Per-query summaries, which reuse `_telemetry.summarize_query_range`, go in
    an extra `metrics` field. The `workload` model allows extra fields
    (`extra="allow"`). Top-level `Signals` forbids extras, so the extra field
    can't go at the top level.
  - The record also stores the capture's sha256 digests, so the Signals file
    can be traced back to the capture.
- **R0 result (§1.7 item 5).** The `workload` extras, including `metrics`,
  reach the stage 03 prompt verbatim, and an empty `anomalies` list does not
  skip stage 03. But the stage 03 prompt only tells the model to work "for
  each anomaly". It never mentions workload metrics. So the data arrives
  with no instruction to use it. Whether that gives candidates or
  `no_opportunity` is **not established** without a paid run. Expect mostly
  `no_opportunity`, or candidates that cite no anomaly. The plan does **not**
  add anomaly detection to make up for this.
- **Owner decision (R0-1 → D10, 2026-11-09): (b), hold.** R6a and R6 are
  not built until the upstream Spotlights stage 03 prompt handles
  metrics-only workloads. The options were:
  - (a) build R6a/R6 as planned, and let the R9 telemetry run show what a
    metrics-only Signals file yields. A `no_opportunity` there counts as a
    truthful result, not a failure of R6.
  - (b) hold R6a/R6 until the Spotlights prompt (upstream, outside this
    repo) handles metrics-only workloads.
  - (c) drop the telemetry-driven path from Milestone A.

  Milestone A therefore cannot reach R9's "all three paths" bar until the
  hold lifts. R7, R8 and R9 go ahead for Nous and Spotlights static. Their
  telemetry rows stay open, and the coordinator keeps the telemetry path on
  its legacy per-launch go.
- doctor: `spotlights-telemetry` becomes available when a capture has (or can
  produce) a Signals file. The old F1 reason stays for raw metrics-only
  directories.
- Once Milestone B lands, this conversion is read-only, costs nothing, and is
  not a governed launch. The capture before it is governed in B1. Until then,
  the capture keeps its legacy per-launch go.
- **Tests:**
  - deterministic output
  - the output validates against Spotlights' `Signals` shape (fixture copy of
    the schema rules)
  - no anomalies are ever invented
  - capture digests are carried through

### R6 — Spotlights telemetry-driven adapter

- `validate`:
  - `--telemetry-from` is required and accepted only as a Signals JSON (top-level keys checked, non-empty `workload.workload_id` ≠ `stub-workload`) or a directory containing `traces.jsonl`. R6a's output is the normal source.
  - a raw metrics-only directory is rejected, pointing at `telemetry_to_signals.py` (G10)
  - every input file is digested
- `prepare`: argv fixed as `signal-pipeline --repo T --telemetry-from D --output-folder O --artifacts-dir A --to-stage 04 --no-projecttree-cache [--model M]`.
- `collect`: passes `<A>/spotlight_report.json` to the collector (G2) and applies the outcome rules from exit code + `status.json` + stdout `issues`.
- **Tests:**
  - stage 05 cannot be requested in any form (G5); this is a property test over argv construction
  - missing, metrics-only and stub inputs are rejected
  - `no_opportunity` vs `failed`

### R7 — Results and evidence integration

- `results.py`, `runtime.py result`, and validator status recorded through `validate_answer.py` (run as a subprocess, unchanged).
- Collection and validation failures keep the execution record and native output, and never rerun anything.
- **Tests:**
  - negative and inconclusive outcomes are `completed` executions
  - partial results are labelled
  - `unavailable` classification has a reason
  - `answer.json` contract and validator tests are unchanged and still pass

**Status: done for Nous and Spotlights static (2026-11-09); telemetry pending D10.** Code:
`ains_runtime/results.py`, `runtime.py collect|result` (`cli.py`), `results/` in `export.py`,
and the result validator in `contracts.py`. Tests: `tests/test_runtime_results.py`, plus more
malformed cases in `test_runtime_contracts.py`. `test_answer_contract.py`, `test_validate*.py`,
`validate_answer.py` and `doctor.py` are unchanged. Deviations:
- **A fourth axis, `collection{state: not_run|collected|unavailable|failed, reason}`.** It sits
  beside execution, outcome and validation, so a collection failure is recorded without touching
  `execution`. `validation.state` is only the validator's result. contracts.py requires it.
- **`outcome.classified_by: runtime|coordinator`.** The runtime never infers a verdict. Nous is
  `unavailable` with Nous's reason. Spotlights `candidates_found`/`no_opportunity` are `other`
  with an explanation. Anything not completed and collected is `unavailable`. The coordinator can
  pass `result --outcome C --explanation T`. It is accepted only for a completed, collected run,
  and supported/not_supported/inconclusive only when the request has a `hypothesis`
  (`hypothesis_ref` = `<request message id>:body.hypothesis`).
- **Findings on failure (D3).** Allowed only when the run is failed/cancelled, collected, and
  `findings.partial: true` (a Nous halt). Otherwise only for completed runs. `findings` now
  carries `partial`, `claims` (only the five contract keys), optional `hypotheses` (Nous,
  per iteration), `candidates` (Spotlights ids/locations, no estimates) and `answer_ref`.
- **Estimate guard, applied twice.** The results normalizer rebuilds the
  `collect_spotlights.estimate_values` set from native output. If a claim value matches it, the
  collection is `failed` and the record has no findings, even when the adapter's own guard
  passed.
- **Validation.** `validate_answer.py <answer> [--root] --check-citations --repo <name>=<target>`
  is run as a subprocess. The answer is `--answer`, else `<study>/final/answer.json`, else the
  engine's own answer.json (Nous, `--root` = work dir). Output goes to
  `.runtime/results/<inv>.validation-<n>.txt` and is linked as `report_ref`.
- **Storage.** Each `collect`/`result` appends a `collect:result`/`result:built` event (no new
  table, store schema stays v1). `export` writes the latest result per invocation. `result`
  reconciles first (as `status` does), refuses a non-terminal invocation, and builds a
  record for `recovery_required`/`not_launched` (`collection: not_run`). Nous `collect` also
  merges the collect.py bundle into `<study>/collection.json["nous"]`, as `collect.py --study`
  does. The study defaults to `<target>/investigations/<slug>`, if that directory exists.
- **Plug-in point for R6.** Normalization runs the adapter's `normalize_collection()` if it has
  one, else the `NORMALIZERS[capability]` entry, else `generic()`. A telemetry adapter can
  therefore plug in without changing results.py.
- **For R8 to document:** the `collect`/`result` commands and their flags, the four axes, when the
  coordinator gives `--outcome`, and that a validation `failed` means fixing the answer, never
  rerunning the experiment.

### R8 — Coordinator, references, doctor

- SKILL.md, `method-plan.md`, `nous-blis.md`, `spotlights.md` and `plan.md.tmpl` changes (§4.7, plus G1/G2/G3/G6 doc fixes). Add `runtime-protocol.md`.
- doctor: runtime version and DB schema.
- **Test:** a grep-based test that no SKILL.md or reference text tells the agent to launch a migrated engine directly, outside `runtime-protocol.md`'s "what the adapter runs" section.

**Status: done for Nous and Spotlights static (2026-11-09); telemetry pending D10.** Docs:
SKILL.md (new hard gate 2, steps 2–5 and 7 rewritten, `metadata.runtime-version`), new
`references/runtime-protocol.md`, `method-plan.md`, `nous-blis.md`, `spotlights.md`,
`telemetry.md`, `assets/plan.md.tmpl` (provisional outline, `## Launch gates`, `## Step log`).
G1, G3 and G6 are fixed in the references. Code: `doctor.py` gains a `runtime` block. Tests:
`tests/test_runtime_docs.py`, more cases in `tests/test_doctor.py`. Deviations:
- **Telemetry stays legacy (D10).** `signal-pipeline` and `capture_prom.py` keep their per-launch
  go, and both files label it "legacy gate, not runtime-enforced". The grep test's
  `LEGACY_ALLOWLIST` has two entries, `(references/spotlights.md, signal-pipeline)` and
  `(references/telemetry.md, capture_prom.py)`. The test also fails on a stale entry, so B2 must
  remove each entry together with its launch.
- **G2 is documented, not coded.** R6 is on hold, so the collector has no `--result` flag.
  `spotlights.md` says the report is under `--artifacts-dir`, and tells the agent to pass that dir
  as `--run-dir` with `--sha`.
- **What the grep test checks.** No fenced command in SKILL.md, references or assets runs
  `nous run|resume|stop` or `spotlights-engine`, except in runtime-protocol.md's "What the adapter
  runs" section. Env prefixes and `\` continuations are handled. An inline mention of a migrated
  launch must sit next to a negation (never/not/only through, within two lines or under the
  heading). Stage 05 (`--to/only/from-stage 05`, `--inject 05`, `run_pipeline`) is never in a
  command, and every prose mention of it is a prohibition.
- **No-chaining coordinator test (spec §14).** The test checks three things. The docs must say
  stop / clarify / a separately approved request. An AST check over `results`, `recovery`,
  `export`, `limits`, `artifacts` and the adapters confirms none of them calls or imports
  `launch`, `reserve*`, `spawn_supervisor`, `supervise`, `prepare`, `revalidate`, `submit` or
  `approve`. The same check covers the CLI `collect`/`result`/`status`/`recover` handlers. Last, a
  real Spotlights static `candidates_found` result is built under R7's `NoLaunch` guard, and the
  test confirms it created no message, work item, preparation, approval or invocation.
- **Examples.** `runtime-schemas/examples/experiment-request.json` is now a block-size Nous request
  that passes R4's semantics and representability checks. The old one varied scorer weight,
  which is not BLIS-representable. `experiment-request-next-step.json` now carries
  `models{claude, codex}` and `limits.max_cost_usd` for R5. A test runs both through their
  adapters' request checks.
- **doctor.** `runtime{present, version, store_schema_version, message_schemas, export_schema,
  pin, start_time_method, available, reason?, warnings}`. The constants come from `ast.parse` of
  the package `__init__.py`, and the module is never imported. The start-time method mirrors
  `procid.method()`. When the runtime is unavailable (files missing, no start-time method, no
  PyYAML), `nous-blis` and `spotlights-static` are unavailable with a "runtime unavailable" reason,
  and `sim2real-prep` follows `nous-blis`. `methods` has no `runtime` key. A pin mismatch is a
  warning. The `ains_runtime` name is split in doctor.py's source because R1's isolation test
  forbids the literal name there.
- **Not done:** `install-smoke` still runs doctor only. The runtime smoke test is R9.

### R9 — Milestone A acceptance

- An acceptance test matrix (`tests/test_runtime_acceptance.py`), mapping each spec §14 bullet to named tests (table below).
- `install-smoke` is extended: install into a throwaway repo, then `runtime.py submit` + `prepare` against the fake adapter. This proves there are no checkout-specific paths and no Hive/Crux dependency.
- **Real integration runs. These cost money and each needs the owner's own approval:**
  - one Nous campaign (small `max_iterations`)
  - one Spotlights static run on one module
  - one Spotlights telemetry run on a Signals file R6a produced from a real Prometheus capture (D6)

  Each goes through `prepare → owner approve → launch → collect → result`, and the exported records are kept in an example study. Mocked tests alone are not evidence that the runtime works with the real engines (spec §14).

| Spec §14 acceptance bullet | Covered in |
|---|---|
| Approved preparation launches via real adapter; material mutation rejected | R4/R5/R6 unit tests + R9 real runs |
| Spotlights preserve artifacts, distinguish no-opportunity vs failure, no estimate as claim | R5, R6 |
| Spotlights result → stop / clarify / separately approved Nous request; no auto-chaining | R8 coordinator test + R3 (supervisor never chains) + R9 |
| Telemetry path rejects bad inputs; no stage 05 | R6 |
| Duplicate delivery / concurrent equivalents don't start duplicate jobs | R1, R3 |
| Crash before and after spawn → known state or reconciliation, no relaunch | R3 |
| Missing requirements / version mismatch fail before launch | R2, R4–R6 |
| Cancellation and partial execution represented truthfully | R3, R4–R6 |
| Negative/inconclusive remain successful executions | R7 |
| Collection/validation failure preserves evidence, no rerun | R7 |
| Next step needs fresh justification + approval; stopping needs no launch | R2, R8, R9 (`next_step` enforced at prepare) |
| Installs into separate repo, no Hive/Crux, no checkout paths | R9 install-smoke, existing `no-hardcoded-paths` |
| Existing validation, tracking modes, prohibited actions intact | existing test suite unchanged + R8 grep test |

**Status: no-cost parts done (2026-11-09); real runs pending the owner's approval;
telemetry rows open under D10.** Milestone A is **not** complete.
- **Acceptance matrix:** `tests/test_runtime_acceptance.py`. `MATRIX` maps every §14
  bullet to named tests, and a test checks that each one resolves by dotted name. A
  second test checks that the rows cover the spec's 13 bullets. The three telemetry rows
  (launch/mutation, artifacts/no-opportunity/estimates, bad inputs/stage 05) are
  `open-D10`. They are reported as **skipped** tests (`TelemetryOpenUnderD10`, reason
  "D10: R6a/R6 on hold"), never as passes. The test also checks that no telemetry
  adapter is registered.
- **Gap found and closed: next-step justification.** §4.1 said `next_step` is "required
  whenever the study already has a result", but nothing enforced it. `prepare` now
  refuses a request without `next_step` once the study has an invocation in
  `completed|failed|cancelled` (`preparation.next_step_problems`). The approval card shows
  the justification under "Next step justification". It stays non-material. Tests:
  `NextStep`, `SpotlightsToNous` (a Spotlights candidate becomes a Nous request: refused
  without `next_step`, then its own card and code, unlaunchable until approved). Two R7
  tests that prepared a second request in the same study now carry `next_step`.
- **Install smoke:** `tests/_runtime_install_smoke.py`, a test-only driver that is not
  shipped. It runs in the `install-smoke` job of both `scripts/ci-local.sh` and `ci.yml`.
  After `npx skills add`, it checks the install: only the shipped adapters are present,
  there is no adapter-loading hook (`importlib`, `entry_points`, `__import__`, ...), and
  no file contains the checkout path or imports Hive/Crux. It commits the install, runs the
  **installed** `runtime.py submit` in a subprocess (submitting twice gives the same
  receipt), then imports the **installed** `ains_runtime`, registers the fake adapter
  in-process and calls the installed CLI's `prepare`. It also checks that the card is
  written under the temp runs dir with no checkout path, that the target stays clean, and
  that no Hive/Crux module is loaded. The shipped CLI still has no way to load an adapter.
- **Found by the install smoke: bytecode dirtied the target.** A project-level install
  sits inside the target repo, so the first `runtime.py` import wrote `__pycache__/` there,
  and every `prepare` then refused the dirty tree (D4). Every shipped entry script that
  imports siblings now sets `sys.dont_write_bytecode = True` first, and the runtime runs
  `validate_answer.py` (unchanged) with `python -B`. Test:
  `InstallSmoke.test_shipped_entry_points_write_no_bytecode`. D4 itself is unchanged.
- **Real-run material:** `examples/runtime-milestone-a/README.md` gives the two
  plain-language prompts (Nous, BLIS block size 16 vs 64, one iteration; Spotlights static
  on `pkg/kvcache/kvblock`) and the setup (global install, clean target, optional
  `models` defaults). It has no request files: the owner never writes structured input.
  SKILL.md makes this a rule, `runtime-protocol.md` "Where each field comes from" says
  where the agent gets each field, and the machine config gains an optional `models`
  key for default model ids. An earlier scratch `prepare` of a Nous request in a
  throwaway clone passed every check (engine pin, binary sha256, catalog commit,
  collisions, clean tree). No engine was run and nothing was approved or launched.
- **Still open:** the Nous and Spotlights static real runs, each with the owner's own go,
  with their `runtime/` exports kept in the example dir (the matrix test fails once
  results appear, so it gets updated with them). The telemetry run and rows wait on D10.

---

## 6. Phase plan — Milestone B

### B1 — Telemetry capture governance

- A `telemetry_capture` adapter around `capture_prom.py` (argv, not import).
- **Material fields:** cluster name, kind, endpoint/context (no token), absolute window, step, query set digest, and the token env var *name* recorded as present or absent.
- **Limits:** a size guard, already engine-enforced through `size_guard()`. No count cap (D8).
- **Result mapping:** `completed` with capture id, `failed` with the reason (port-forward, HTTP, empty result), and the outcome `other` ("snapshot captured").
- Cancellation: SIGTERM, since `capture_prom.py` already handles the port-forward teardown. R0-style check first.
- Remove the "legacy gate" label from SKILL.md for telemetry.

### B2 — Migration completion

- Demonstrate result-driven optional transitions (telemetry → Spotlights telemetry → Nous, and stopping at each point) in an example study, with a separate approval for each launch.
- Reconcile README, `design-v2.md` and `ains-runtime-spec.md`/`runtime-spec-comparison.md`, labelling historical claims as historical (spec §5).
- Publish `runtime-protocol.md` + schemas as the versioned local handoff contract, with compatibility rules (spec §13). No Hive/Crux code.

**Done when:** every in-scope expensive launch has a governed path, and the R8 grep test runs with an empty allowlist of legacy direct launches.

---

## 7. Owner decisions (2026-11-09)

| # | Decision | Owner's answer | Spec impact |
|---|---|---|---|
| D1 | How approval is given | **In chat, through the owner's agent.** The approval card is shown verbatim, the owner gives the go and quotes the short code, and the record is single-use, bound to the digest, with a quoted source. It records consent; it does not prove who gave it. | **Amend spec §7** |
| D2 | Nous `PARTIALLY_CONFIRMED` | `other` + Nous's `partial_note` | — |
| D3 | Nous `halt` | `failed` execution; findings kept, labelled `partial` | — |
| D4 | Clean-tree check | Ignore `investigations/` and `.nous-experiments/`; any other change blocks | — |
| D5 | Spotlights objective-setting / sort-candidates | Stay in-session, not governed launches | — |
| D6 | Telemetry-driven input | The skill reshapes Prometheus captures into Signals JSON (R6a), with **no anomaly logic**. Spotlights accepts metrics-only Signals, and traces are optional. | Partly revises v2-plan F1 |
| D7 | Time limits | Advisory only | — |
| D8 | Invocation-count caps | **None.** Per-run approval and the duplicate check are the guards, and counts are shown on approval cards. | **Amend spec §9** |
| D9 | Env vars as material | Only the explicit model setting is material. `ANTHROPIC_*`/`CLAUDE_*` values are recorded and warned about on change, but don't invalidate an approval. | **Narrow spec §7** "relevant environment configuration" |
| D10 | R0-1: metrics-only Signals vs the anomaly-driven stage 03 prompt | **(b) Hold R6a/R6** until the upstream Spotlights stage 03 prompt handles metrics-only workloads. R7–R9 go ahead for Nous and Spotlights static; the telemetry rows of R9 stay open. | Milestone A stays incomplete (spec §14) until the hold lifts |

The spec amendments (D1, D8, D9) go in the same PR as R1, so code and spec
never disagree.


## 8. Out of scope (from spec §3)

Out of scope: distributed queues, a workflow engine, arbitrary command
execution, automatic expensive retries, automatic capability chaining,
deployment, PR creation, and Hive/Crux integration. Signal-pipeline stage 05
stays prohibited, and engine directories stay read-only to the coordinator,
the runtime and the collectors. The runtime enforces rules on requests sent
through it, but it is not a sandbox. Nous's own `bypassPermissions` session
is one concrete reason.
