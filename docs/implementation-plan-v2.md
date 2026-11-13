# Investigation skill v2 — implementation plan

Status: **draft for owner review** (2026-11-05). Implements `docs/design-v2.md`.

This plan breaks the design into eight PR-sized phases. Each one leaves `main`
green, meaning tests pass and the skill installs. It also lists the places
where the design and the code disagree, which need a decision before or
during implementation.

## Findings from reading the code (resolve before or during the phase noted)

| # | Finding | Affects | Recommendation |
|---|---|---|---|
| F1 | **signal-pipeline can't read a metrics-only directory.** Stage 01 (`spotlights/src/spotlights_engine/signal_pipeline/stages/s01_signal_extraction.py:50`) only takes the raw-OTel route when `traces.jsonl` exists. Otherwise it needs a pre-cooked `01_signals.json` / `signals.json`. The design says `metrics.jsonl` "reads with no adapter", and that's false. | Phase 2, 3, 4 | **Decided (owner):** spotlights owns anomaly detection, not this skill. The skill doesn't write `01_signals.json` and doesn't fake a `traces.jsonl`. `capture_prom.py` still writes `metrics.jsonl` in OTel `resourceMetrics` form, so it's ready when spotlights supports it. Until spotlights accepts a metrics-only directory, `doctor.py` reports `spotlights-telemetry` as unavailable for metrics-only captures and gives this reason. Owner action: raise the gap with spotlights (metrics-only `--telemetry-from`). |
| F2 | **"Stdlib-only" isn't true today.** `collect.py` imports PyYAML, and every config file (`config.yaml`, `promql/starter.yaml`, `campaign.yaml`) is YAML. | Phase 2–4, frontmatter | **Decided (owner): yes.** Allow exactly one dependency, PyYAML. `doctor.py` and `validate_answer.py` stay stdlib-only, so preflight can report a missing PyYAML instead of crashing on it. Change the `compatibility` line to "Python 3.10+ and PyYAML". |
| F3 | **No git remote is configured.** `npx skills add AI-native-Systems-Research/groundwork` and CI both need the GitHub repo. | Phase 7 | **Resolved 2026-11-10:** the repo is published (github.com/AI-native-Systems-Research/groundwork), so remote installs and CI both work; the earlier "publish later" decision was overtaken. |
| F4 | **`--check-citations` header names a repo, but the validator needs a path.** `repo@sha:path:Lstart-Lend` only gives a repo *name*. | Phase 5 | Resolve a name in this order: (1) `--repo NAME=PATH` flags; (2) the git toplevel that contains the answer file, if its remote basename matches; (3) a `repos:` map in `~/.config/investigation/config.yaml`. If none match, it's an error under `--check-citations`. It's never a silent pass. |
| F5 | **The current global install is a copy.** `skill/build.sh` copies the skill into `~/.claude/skills/investigation/`. Once build.sh is deleted, that copy goes stale with nothing to update it. | Phase 1 | In the same PR: `rm -rf ~/.claude/skills/investigation` and reinstall with `npx skills add ./ --skill investigation -g` (owner-run, touches $HOME). |
| F6 | **The step count is inconsistent.** The design says "SKILL.md keeps the 9 steps" but the flow has steps 0–9 (ten). | Phase 2 | Use 0–9 throughout. |
| F7 | **Engine pins.** The local spotlights checkout is at `e7aff25`, an unpushed `skill/spotlights-run` branch. `v0.1.1` is the release. | Phase 2 | `doctor.py` warns about this exact case, and it becomes the first real test of the warning. |
| F8 | **Existing study evidence uses absolute `/Users/kelly/...` paths** (e.g. `studies/pd-threshold-retro/final/answer.json`). | Phase 1 | As the design says: leave them alone, and add a note in `examples/README.md`. Tests must not validate the examples. |

## Phase plan

Dependencies: P1 → P2 → (P3 ∥ P4 ∥ P5) → P6 → P7 → P8. P3, P4 and P5 touch
separate files and can run in parallel, but P4's telemetry collector needs
P3's capture format, so pin that format first (P3, step 1).

### P1 — Restructure (pure moves, no behavior change)

- `git mv scripts/{collect.py,validate_answer.py,make_manifest.sh} skills/investigation/scripts/`
- `git mv docs/answer-contract.md docs/hand-scoring.md skills/investigation/references/`
- `git mv studies examples` and add `examples/README.md` (paths point at the old layout, F8).
- `git rm -r skill/ skills-src/`. The current generated `skills/investigation/SKILL.md` becomes the hand-edited source, keeping its v1 body for now.
- `tests/test_*.py`: `REPO / "scripts"` → `REPO / "skills/investigation/scripts"`.
- `.gitignore`: keep `runs/` for now (removed in P6).
- Fix every link to the moved docs (`grep -rn 'docs/answer-contract\|docs/hand-scoring\|studies/'`).
- Reinstall the global copy (F5).

**Done when:** `python3 -m unittest discover tests` passes with zero test-body changes, and `git diff --stat -M` shows renames only, apart from the test path lines and link fixes.

### P2 — Preflight, scaffolding, config, SKILL.md skeleton

New files under `skills/investigation/`:

- `scripts/doctor.py` (stdlib only, read-only). It reports:
  - Python version, PyYAML, git, kubectl, npx.
  - The session repo toplevel, its remotes, and which remote is upstream vs fork.
  - Both config layers: present, parseable, and the keys they set.
  - Engines: `nous`, `spotlights-engine`, `signal-pipeline` (inside spotlights), sim2real. For each: found, version/sha, the pin from the SKILL.md frontmatter, and the latest upstream tag (`git ls-remote --tags`, network-optional and skipped offline).
  - The BLIS apparatus root, the runs dir, and free disk space.

  Output: human text by default, `--json` for the agent. The core field is
  `methods: {code-reading: {available: true}, nous-blis: {available: false, reason: "no BLIS apparatus configured"}, …}`.
  Exit 0 always, unless the session isn't in a git repo.
- `scripts/new_study.py --slug S [--question TEXT]`:
  - creates `investigations/<slug>/` from `assets/`;
  - fills `question.md` with remote URL, branch, `HEAD` sha, and engine versions taken from `doctor.py --json`;
  - on the first study in a repo, prompts for the tracking mode (`fork` / `untracked`) and writes `investigations/config.yaml`, or `~/.config/investigation/repos/<repo>.yaml` in untracked mode, plus the `.git/info/exclude` line;
  - provides `--commit-guard`, which refuses when the current branch's push remote is the upstream remote. SKILL.md tells the agent to run it before any commit that touches `investigations/`.
- `scripts/_config.py`: a shared loader for both layers plus defaults (`INVESTIGATION_RUNS`, etc.). It imports PyYAML lazily, so `doctor.py` can import the rest without it.
- `assets/question.md.tmpl`, `plan.md.tmpl` (sections from the design table), `hand-score.md.tmpl`, `config.yaml.tmpl`.
- `SKILL.md`: the new frontmatter from the design, plus the gates, steps 0–9 as short paragraphs (one pointer each to `references/…`), the "never" list, and the rule "resolve `scripts/` against this file's folder". Remove every `/Users/kelly` path. Target: well under 500 lines.
- `references/method-plan.md` (method menu, BLIS representability table moved from v1, pushback rules, cost table) and `references/nous-blis.md` (v2 authoring facts, A1 apparatus, pre-launch checks, launch command with `NOUS_CAMPAIGN_PARENT=$INVESTIGATION_RUNS/<repo>/<slug>`). These are mostly moves from the v1 body.

Tests:
- `test_doctor.py`: fake `$HOME` and fake git repos in a tempdir, a missing-engine case, and `--json` shape.
- `test_new_study.py`: scaffold, fork vs untracked, and the commit guard refusing on an upstream push remote.

**Done when:** the tests pass; `doctor.py` run in `~/git/llm-d-router` lists methods correctly and warns on the spotlights pin (F7); `grep -rn /Users/ skills/` finds nothing.

### P3 — Telemetry capture

1. **Pin the snapshot format first** in `references/telemetry.md`:
   - `capture.json` schema;
   - the `prom/<name>.json` layout, which is the raw `query_range` response;
   - the `metrics.jsonl` OTel mapping (one `resourceMetrics` per query × series; Prometheus labels → resource/attributes; points as gauge `dataPoints`);
   - the size guard.
2. `scripts/capture_prom.py`, exactly the CLI in the design:
   - `--start/--end` are required;
   - the step is auto-checked against the 11k-points-per-series limit (error with a suggested step, never a silent clamp);
   - the bearer token is read from the env var only, with a test proving it never reaches disk or stdout;
   - `--port-forward` starts `kubectl port-forward` with a random local port, waits until it's ready, and kills it in `finally`;
   - an empty result is recorded in `capture.json.empty_queries`, never treated as a failure;
   - when a capture exceeds 50 MB, the bulk files go to the runs dir and the study keeps `capture.json` (with sha256 of the moved files) plus `summary.json`;
   - stdlib `urllib` only.
3. `assets/promql/starter.yaml` from the design. **Metric-name check:** grep `vllm/v1/metrics/` @ `2f1cba799e` and llm-d-router @ `618eb0a9` for each name, `_bucket` suffix, and label (`plugin_type`, `status`, `decision`). Record the check in a comment header at the top of the file. Fix or drop names that don't exist.
4. Tests:
   - a recorded `query_range` fixture served by a stdlib `http.server` in-thread;
   - the step-limit error;
   - an empty query;
   - the token-leak check;
   - size-guard routing (shrink the threshold through an env var for the test);
   - a stubbed `kubectl` on `PATH` for port-forward teardown.

**Done when:** the tests pass, and one real capture against a cluster the owner names runs end to end (owner go needed: it touches an external system).

### P4 — Collectors

- `collect.py`: add `--runs-dir` (default from `_config.py`). Behavior stays the same, and the existing tests stay unchanged.
- **Decided (owner): yes.** `collect.py`, `collect_spotlights.py` and `collect_telemetry.py` **merge** into `collection.json` under their own top-level key (`nous`, `spotlights`, `telemetry`), keeping other keys. Today `collect.py` overwrites the file. Keep its old top-level shape under `nous` and bump a `collection_version: 2` field. Update `test_collect.py`'s collection.json assertions to the namespaced shape. This is the only allowed test change outside P1, and it should be called out in the PR.
- `collect_telemetry.py --study S --capture-id ID`:
  - reduces each `prom/*.json` to `{mean, p50, max, min, n_points, n_series}` per series and overall, written to `summary.json`;
  - does **not** detect anomalies or write `01_signals.json`; that's spotlights' job (F1);
  - drafts measured claims whose `condition` already names cluster, window and query.
- `collect_spotlights.py --study S --run-dir R`:
  - copies `sorted_candidates.md`, `result.json` and the manifest into `spotlights/`;
  - drafts code-citation claims for the top-N candidates by writing `citations/<id>.txt` from `git show <sha>:<path>`, so the quote is verbatim by construction;
  - never turns a Spotlights *estimate* into a claim value. A test asserts this.
- `references/spotlights.md`: static and telemetry usage, "never run stage 05", the metrics-only caveat, and what to keep.
- Tests use small fixture `result.json` and `prom/` directories.

### P5 — Validator: `--check-citations`

- Parse the header `repo@sha:path:Lstart-Lend`, resolve the repo (F4), run `git -C <repo> show <sha>:<path>`, and compare lines start..end byte-for-byte with the quoted body (trailing-newline tolerant only).
- Only claims whose evidence lives under `citations/` are checked. Without the flag, behavior is identical.
- Tests: valid citation, wrong lines, stale sha, unknown repo, and missing header, all built against a tempdir git repo created in the test. **`test_validate.py` must pass unchanged.**
- `references/answer-contract.md`: append an "Evidence kinds (v2)" section *below* the verbatim block. Add a test that the verbatim block still matches its sha256, so it can't drift.

### P6 — Remaining references, docs, cleanup

- `references/sim2real.md`: algorithm vs BYO bundles, the printed `sim2real translation register` command, never deploy.
- `references/hand-scoring.md`: add item 11, "was the method plan right in hindsight?".
- Delete `docs/hive-mapping.md` and the README's Hive note. Remove `runs/` from `.gitignore`, since runs now live outside the repo.
- Rewrite `README.md`: what it is, install lines, a 5-step quickstart, layout, credits (mlsys contract by mtoslalibu, Nous, Spotlights, sim2real, signal-pipeline).
- Add `LICENSE` (Apache-2.0, confirmed by the owner).
- Add a short note at the top of `docs/build-log.md` saying v2 is in progress.

### P7 — CI (GitHub Actions; needs F3)

The repo is published (F3, resolved 2026-11-10), and the workflow runs on
GitHub. The jobs also run locally through a `scripts/ci-local.sh` wrapper (not
installed with the skill), which stays the fast path while iterating.

- `unit`: Python 3.10 and 3.12, `pip install pyyaml`, `python -m unittest discover tests`.
- `spec`: `skills-ref validate skills/investigation`. Check the actual package name and install method first; if no CLI is published, add a small frontmatter and line-count check script.
- `install-smoke`: a temp `git init` repo, then `npx skills add $GITHUB_WORKSPACE --skill investigation -a claude-code -y`, then assert that `.claude/skills/investigation/{scripts,references,assets}` exist and that `python3 .claude/skills/investigation/scripts/doctor.py --json` exits 0. This is the check the design asked for: does `skills add` copy the whole directory? **Run it locally in P1** too. If it only copies SKILL.md, the packaging decision has to be revisited before P2.
- `no-hardcoded-paths`: fail on `/Users/` under `skills/`.

### P8 — Dogfood (owner-gated)

Run two small studies with the installed skill, from inside `~/git/llm-d-router`:

1. **Code-reading only.** Re-ask "what's the default prefix block size and where is it set?". This exercises intake, the plan, pushback ("pure lookup"), a code citation, and `--check-citations`.
2. **Telemetry.** E.g. "is prefix reuse on cluster X high enough to matter?". This exercises a capture go, `collect_telemetry`, and measured telemetry claims. Add signal-pipeline to stage 04 only if spotlights accepts metrics-only input by then (F1). Otherwise `plan.md` should list it as unavailable, which is a check in itself.

Hand-score both, and feed any defects back as fixes before calling v2 done. A
Nous re-run isn't needed: the blocksize-sweep example already covers that path,
and its logic is unchanged apart from paths.

## Effort and order of work

| Phase | Size | Notes |
|---|---|---|
| P1 | S | half a day; includes the local install-smoke check |
| P2 | L | biggest: doctor, new_study, config, SKILL.md rewrite |
| P3 | M | metric-name verification is the slow part |
| P4 | M | collection.json namespacing is the only behavior change to existing code |
| P5 | S | |
| P6 | S | |
| P7 | S | done: repo published 2026-11-10, CI runs on GitHub |
| P8 | M | owner time + one real cluster capture |

## Owner decisions (2026-11-05)

1. Anomaly detection from telemetry: **spotlights' job, not the skill's** (F1).
2. PyYAML as the single dependency: **yes** (F2).
3. Namespaced `collection.json`: **yes** (P4).
4. License: **Apache-2.0** (P6).
5. Publishing to GitHub: **later**; local installs and local CI until then (F3, P7). *(Overtaken: the repo was published 2026-11-10.)*

Still open: raising the metrics-only `--telemetry-from` gap with spotlights (owner).
