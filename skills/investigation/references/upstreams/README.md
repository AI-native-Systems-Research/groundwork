# Upstreams: the locked versions and what we rely on

Everything this skill knows about another project lives in this folder. Code
doesn't restate any of it, and `tests/test_upstreams.py` enforces that.

| File | Holds |
|---|---|
| `lock.json` | The exact versions used: the Nous, Spotlights, sim2real, vLLM and llm-d-router commits; the BLIS catalog commit; every BLIS binary by sha256, with its source commit, embedded router and patches |
| `nous.json` | Nous facts: campaign keys, task types, effort levels, stop reasons, native files, exit code, env var, worktree dir |
| `spotlights.json` | Spotlights facts: file env vars, model id patterns, module-name rules, dry-run fields, exit codes |
| `blis.json` | BLIS facts: which knobs are representable and which aren't (`method-plan.md`'s table is generated from this) |
| `sim2real.json` | sim2real facts: the Python floor, declaration-only packaging, register spec form, subcommands |
| `vllm.json` | vLLM facts (metric-name source only): counter/histogram classes, which counters are scraped with `_total`, the `pod` label caveat |
| `llm-d-router.json` | llm-d-router facts (metric-name source only): which counters declare `_total` themselves, the `decision_type` label and its values |

vLLM and llm-d-router are locked as **metric-name sources only**: nothing of
either is installed or launched, and their lock entries exist so the
Prometheus names in `assets/promql/starter.yaml` and `references/telemetry.md`
can't drift from versions nobody re-checked. sim2real is **checkout-only**:
its `pyproject.toml` is declaration-only, so the pin names the commit a
configured checkout (doctor's `engines.sim2real`) is compared against, not an
installable version. The version banners in `references/*.md` and
`assets/promql/starter.yaml` must cite commits this folder holds —
`tests/test_upstreams.py` `test_banner_shas_are_locked` enforces it — with one
documented exception: an unpushed engine branch (see docs/design-v2.md's pin
policy) is never pinned, and its banner must say so inline, the way
`spotlights.md`'s `e7aff25` does.

Each fact is `{"value": ..., "source": "<path:line at the locked version>"}`.
Each facts file has `verified_against: <name>@<12 hex>`, a digest of its
`lock.json` entry. Edit the entry and the facts file goes stale: the
runtime refuses to load it and the tests fail until it's re-verified.

## Bumping an upstream

1. Install or build the new version.
2. Edit its entry in `lock.json`. For a new BLIS build, add it under
   `binaries` with its sha256, source commit, router and patches.
3. Run `python3 scripts/verify_upstreams.py`. It checks the installed engine
   shas, the binary hashes, every knob flag against each binary's
   `blis run --help`, and the catalog commit.
4. Re-check each fact in that upstream's facts file against its `source`
   at the new version. Update the values and citations.
5. Set `verified_against` to the new id. The failing test prints it, and
   `verify_upstreams.py --json` lists it under `lock_ids`.
6. If `blis.json` changed, run `python3 scripts/verify_upstreams.py --write-docs`.
7. Run the tests.

Only steps 2, 4 and 5 are edits, and they all happen in this folder. Code
changes only when an upstream gains something the adapter has to handle
in a new way.

## Adding a fact

Add it to the facts file with a `source`, and read it with
`upstreams.fact(name, key)`. Don't copy it into code or docs.
