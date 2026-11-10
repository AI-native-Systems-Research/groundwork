# Hand-scoring checklist

This checklist is filled in by the owner (Kelly) at the end of every study.
Copy this file into the study as `investigations/<slug>/hand-score.md` and score each
item **0, 1, or 2**, with one line of explanation for each.

- **2** — fully met, no hedging needed.
- **1** — partially met, or met with an excuse attached.
- **0** — not met.

## The eleven items

1. **Question** — was it falsifiable, and written down before anything ran?
2. **Grounding** — does `grounding.md` tie the question to real llm-d code, or honestly justify skipping?
3. **Baseline** — was the comparator named before the runs, not picked after?
4. **Apparatus** — did the locked tool (binary sha, catalog commit, any locked analysis script) verify before launch, and stay unchanged after?
5. **Guards** — did the collector's guards pass; and if not, is the failure carried into the answer?
6. **Claims** — does every number in the prose answer appear as a claim?
7. **Evidence** — did the validator exit 0 (every evidence file contains its value)?
8. **Repro** — does running `repro` (or inspecting why not) support the headline number?
9. **Negatives** — are null or negative results reported as results, not smoothed over?
10. **Caveats** — do the known simulator limits appear in the answer text?
11. **Method plan** — was the method plan right in hindsight? Did we over- or under-spend (a Nous campaign where code reading would have answered it, or a lookup that needed a measurement), and should the agent have pushed back at step 2 instead of running anything?

## Score template

| Item | Score | One-line explanation |
|---|---|---|
| Question | | |
| Grounding | | |
| Baseline | | |
| Apparatus | | |
| Guards | | |
| Claims | | |
| Evidence | | |
| Repro | | |
| Negatives | | |
| Caveats | | |
| Method plan | | |
| **Total** | **/ 22** | |
