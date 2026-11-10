# P/D threshold retro — grounding

## Retrospective note

This study is retrospective: the grounding was done by the A1 campaign itself, before
any run. This file points at it rather than re-deriving it.

## Where the lever lives

The P/D disaggregation decider in llm-d-router decides, per request, whether to route
to a prefill or decode instance. The shipped behavior is a prefix-threshold rule: route
to prefill when the request's uncached prefix exceeds a threshold, shipped at **T=16**
tokens. The A1 campaign swept the threshold grid {16, 256, 1024, 2048, 4096, 8192,
16384, 32768, 65536} plus `never`/`always` modes across three layouts (2P/6D, 3P/5D,
4P/4D) and eight workloads, with the shipped `t16` as the control arm.

The mechanism, the grid, the guardrails, and the pre-registered analysis are all
specified in the campaign brief:
`/Users/kelly/git/nous-run/nous-campaigns/blis-pd-decider-threshold/campaign-a1.yaml`
(`target_system.description`), with the locked driver scripts at `scripts/a1/` in the
same root.

## BLIS representability

Confirmed representable: the P/D decider threshold and modes are first-class BLIS
flags (`--pd-decider prefix-threshold --pd-prefix-threshold <T>`), which is why this
question was answerable by simulation.
