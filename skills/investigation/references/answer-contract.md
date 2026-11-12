# The answer contract

## Where it comes from

The contract below is copied word for word from the mlsys study: branch
`origin/mlsys-exp` of `~/git/agentic-strategy-evolution`, file
`experiments/mlsys/tasks/contract.md`, credited to **mtoslalibu**.

We did not invent this shape, and we are not the only ones using it. Nous v2
(branch `origin/nous-v2-fixes`, `orchestrator/v2/schemas/answer.schema.json`)
independently adopted the same schema. That means a study's answer and a
Nous v2 campaign's own `answer.json` speak the same language — one set of
tools, tolerances, and habits covers both.

## The contract, verbatim

> ## Required final output
>
> Before you finish, write the file `final/answer.json` in the repository root, with exactly these keys:
>
> ```json
> {
>   "answer": "short prose answer to the research question",
>   "deliverable": "path to the delivered artifact (patch, script, or null)",
>   "repro": "one shell command, run from the repository root, that regenerates your headline numbers",
>   "claims": [
>     {"id": "c1", "metric": "name of the measured quantity", "value": 0.0,
>      "condition": "the exact configuration it was measured under", "evidence": "path to the file holding this number"}
>   ]
> }
> ```
>
> List every quantitative conclusion you rely on as a claim. Each `evidence` path must exist and contain the value.
> Overwrite `final/answer.json` whenever your conclusions change, so it always reflects your current best answer.

## How we use it

In this repo the answer lives at `investigations/<slug>/final/answer.json`,
scaffolded there by `scripts/new_study.py` (the same run creates
`question.md`, `plan.md` and `hand-score.md`). Every
number that appears in the prose answer must also appear as a claim — if you
wrote a number in a sentence, it needs its own row in `claims`. A claim whose
evidence file cannot be found (or does not actually contain the value) is a
bug, not a claim.

Nobody has to take that on trust. `scripts/validate_answer.py` mechanically
re-opens every evidence file and confirms the value is really there: numbers
are compared with a relative tolerance of 1e-3, strings must match exactly —
the same tolerances as Nous v2's own check. If the validator exits non-zero,
the answer is not done.

## Evidence kinds (v2)

Everything above this section is unchanged; the verbatim block is pinned by
`tests/test_answer_contract.py` (sha256), so it cannot drift. The claim shape
stays the same five keys. What v2 adds is what an evidence file can be:

| Kind | Evidence file | Allowed when |
|---|---|---|
| **measured** (sim) | Nous work-dir artifacts copied into the study | always |
| **measured** (telemetry) | `telemetry/<id>/summary.json` or `prom/<q>.json` | always; `condition` must name cluster, window, query |
| **code citation** | `citations/<claim-id>.txt` | qualitative questions only (set at intake) |

A code citation keeps the contract's five keys:

```json
{"id": "c3", "metric": "prefix-scorer block hashing site",
 "value": "func (db *ChunkedTokenDatabase) TokensToKVBlockKeys",
 "condition": "llm-d-router @ 618eb0a9, pkg/kvcache/kvblock/token_processor.go:L88",
 "evidence": "citations/c3.txt"}
```

### Citation file format

The first line is a header, and everything after it is the quoted source,
verbatim:

```
llm-d-router@618eb0a9:pkg/kvcache/kvblock/token_processor.go:L88-L90
<line 88, exactly as in git>
<line 89>
<line 90>
```

- `repo` is a repo **name**, not a path; `sha` is a commit (abbreviated is
  fine); `path` is relative to the repo root; `Lstart-Lend` is inclusive and
  1-based.
- The body must be lines start..end of `git show <sha>:<path>`, byte for
  byte. Only a trailing-newline difference is tolerated — no reindenting,
  no tab/space changes, no elisions. Writing the file from `git show` (as
  `collect_spotlights.py` does) makes it verbatim by construction.

### `--check-citations`

The plain validator already checks that the claim's `value` string is in the
citation file. `validate_answer.py --check-citations` goes further: for every
claim whose evidence is under `citations/`, it parses the header, runs
`git -C <repo> show <sha>:<path>`, and checks the quoted lines really are at
those line numbers at that commit. That catches fabricated and stale
citations. Without the flag, behavior is unchanged.

The repo name resolves to a checkout, in this order:

1. `--repo NAME=PATH` flags (repeatable);
2. the git toplevel containing the answer file, if its `origin` remote
   basename (minus `.git`) — or its folder name, when there is no remote —
   equals NAME;
3. the `repos:` map in the machine config
   (`~/.config/investigation/config.yaml`, or `$INVESTIGATION_CONFIG_HOME`).
   If PyYAML is missing or the config is bad, this layer is skipped and the
   error says so.

An unresolved name is an error, never a silent pass. Errors (exit 1, one per
line on stderr, `<id>: ...`): header missing or malformed, repo unknown, sha
not found, path not found at that sha, lines out of range, and quoted text
that differs from the source (reported at the first differing line number).

### Spotlights estimates are never claim values

Spotlights *proposals* ("changing X would reduce Y") are hypotheses, not
claims. An answer may describe them in prose, labelled as proposals, but a
number Spotlights estimated never becomes a claim value. Only measured
numbers (sim or telemetry) do; code citations carry quoted code, not numbers.
