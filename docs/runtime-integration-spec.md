# AINS local runtime integration — repository change specification

Status: Accepted by the owner (2026-11-09), with amendments D1 (§7, approval), D9 (§7, environment as material) and D8 (§9, invocation counts) from [runtime-integration-implementation-plan.md](runtime-integration-implementation-plan.md) §7. Implementation proceeds per that plan.
Date: 2026-11-09.

## 1. Purpose and sources

Add a deterministic local execution layer beneath the existing investigation skill. Preserve its method selection, evidence collection, answer validation, and owner review. Close the gaps identified in [runtime-spec-comparison.md](runtime-spec-comparison.md) against [ains-runtime-spec.md](ains-runtime-spec.md).

This document specifies changes to this repository. It incorporates the owner's decisions from the design discussion. It does not claim compatibility with an external implementation or replace Nous's native lifecycle. The current skill and scripts are the implementation baseline; [design-v2.md](design-v2.md) supplies historical design context.

## 2. Settled design decisions

1. The agent chooses and interprets experiments. A deterministic runtime enforces explicit execution conditions. Capability adapters translate inputs and collect outputs.
2. The overall method plan is provisional. After every result, the agent decides whether to stop, ask a question, or propose a justified next step. Each expensive launch needs its own approval.
3. Keep the existing answer contract and evidence validator. Distinguish execution status from scientific outcome and from evidence validation.
4. AINS works locally without Hive or Crux. Portable message contracts may become a shared handoff specification. External integration is optional and is not a dependency or promised compatibility.
5. Deliver Nous and both Spotlights paths together in the first milestone. Neither capability is deferred behind acceptance of the other. Standalone telemetry capture governance follows in the remaining migration milestone.

## 3. Scope and exclusions

The complete migration covers governed launch, status, cancellation, collection, and approved continuation for Nous; governed launch and collection for both Spotlights methods; and approval enforcement for telemetry capture. Code reading, study scaffolding, answer review, hand-scoring, and sim2real preparation remain available.

Do not add distributed queues, a workflow engine, arbitrary command execution, automatic expensive retries, automatic capability chaining, deployment, PR creation, or Hive/Crux integration. Keep signal-pipeline stage 05 prohibited. Keep engine artifacts read-only to the coordinator and collectors.

The runtime is an enforcement boundary for requests submitted through it. It is not a sandbox that prevents an agent with shell access from launching an engine directly. The skill must route migrated actions through the runtime. Stronger host-level access restrictions are outside this change.

## 4. Responsibilities

| Component | Owns | Must not do |
|---|---|---|
| Investigation skill / coordinator | Intake, method choice, provisional plan, next-step rationale, approval presentation, interpretation, final answer | Directly launch a migrated capability; manufacture evidence; treat acceptance as completion |
| Local runtime | Message validation, durable state, approval matching, version checks, duplicate protection, limits, execution records, lifecycle | Choose a hypothesis or claim that an experiment is scientifically worthwhile |
| Capability adapter | Validate native requirements, prepare native inputs, launch through runtime control, collect and normalize output | Choose the next capability, retry on its own, change approved semantics |
| Existing collectors and validator | Extract claims and verify evidence references | Treat a matching value as proof of a sound experimental conclusion |
| Owner | Approve each expensive invocation and review the answer | — |

A runtime can check that a next-step rationale exists. It cannot mechanically establish that the proposed step adds meaningful information. That judgment remains with the coordinator and owner.

## 5. Repository changes

Proposed layout, following the current bundled-script model:

- Add `skills/investigation/scripts/runtime.py` as the runtime CLI.
- Add an adjacent `ains_runtime/` Python package for contracts, persistence, approval checks, execution control, and adapters.
- Add versioned message schemas and runtime protocol documentation under `skills/investigation/references/`.
- Add runtime fixtures and tests under `tests/`.
- Update `SKILL.md`, method references, and the plan template to require explicit replanning and runtime routing for migrated methods.
- Reuse `doctor.py`, `collect.py`, other collectors, and `validate_answer.py`; extract reusable functions where necessary rather than copying their logic.
- Update the README and reconcile the two source design documents after this proposal is accepted. Historical claims must be labeled as historical.

The runtime must work from an installed skill directory against a separate target repository. It must not depend on this checkout's absolute path.

## 6. Local records and contracts

Keep study artifacts under `investigations/<slug>/` and heavy native output in the configured external runs directory. Store runtime state in a dedicated directory under that study's runs directory. Store portable request, prepared-invocation, result, and evidence summaries in the study when needed for review and reproduction. Preserve existing fork/untracked modes and commit guards.

Use a transactional local SQLite store, with a schema version, for work state, unique message IDs, approval consumption, invocation reservations, limits, and event records. Export reviewable JSON records. The database is authoritative for execution bookkeeping; native engine artifacts remain authoritative for capability evidence. Copying a study without its runtime database does not carry permission to execute or replay its requests.

Messages share versioned type, message ID, work ID, revision, optional parent/reply reference, target repository and exact commit, and artifact references. Support experiment request/result, receipt, progress, clarification, and cancellation messages. Reject unsupported schema versions and malformed required fields.

Artifact references include kind, location, and a content digest for immutable artifacts. A local absolute path alone is not a portable artifact identity. Reference mutable logs as provisional until finalized. Never put credentials in messages, prepared records, or logs.

An identical message ID and identical payload returns the existing receipt/state. Reusing an ID with different content is a conflict. Different message IDs with equivalent material invocations must not silently cause another execution.

## 7. Preparation and approval

The required order is:

1. Resolve semantic inputs and explicit defaults.
2. Validate capability availability and native requirements.
3. Prepare the exact invocation and immutable generated inputs.
4. Present its material contents, limits, and defaults to the owner.
5. Record explicit approval bound to that preparation.
6. Revalidate inputs and versions, reserve execution atomically, then launch.

Preparation precedes approval. This corrects the ordering ambiguity in the original runtime spec.

The preparation record includes adapter and capability versions, target identity and commit, question, task type, objective, baseline where applicable, locks, generated input digests, working directory, argument vector, allowlisted environment configuration, expected artifacts, and execution limits. Use argument vectors instead of shell-interpolated command strings.

Fingerprint canonical material content. Material changes include the question, repository/commit, baseline, objective, locks, generated experiment semantics, execution limits, adapter/capability version, and relevant environment configuration. Run IDs and output paths may vary only when the adapter proves they do not change experiment semantics. The adapter owns an explicit, tested material-field policy; do not exclude fields solely because they look like metadata.

*Amendment (owner decision D9, 2026-11-09).* "Relevant environment configuration" is narrowed. It means the environment variables and files each adapter lists as material in its tested material-field policy (for example `NOUS_CAMPAIGN_PARENT`, the `SPOTLIGHTS_*` variables and digests of the files they name, and digests of model-config files). For model selection, only the explicit model setting in the request is material. The values of non-secret `ANTHROPIC_*` and `CLAUDE_*` variables are recorded in the preparation for the record but are not material: launch warns, and does not refuse, when they differ from what was prepared. Variables whose names match `KEY|TOKEN|SECRET|PASSWORD|AUTH` are recorded as present or absent only, never by value or hash. Accepted gap: a changed endpoint or similar setting after approval is warned about, not blocked.

For the local CLI, approval must come from an owner-facing confirmation path that displays the prepared summary and digest. A noninteractive integration requires a documented trusted approval bridge. An agent-written boolean in a request is insufficient.

*Amendment (owner decision D1, 2026-11-09).* The owner-facing confirmation path is the owner's agent chat, in every supported host. `prepare` produces an approval card (material contents, limits, inferred defaults, the full material digest and a 12-hex short code); the agent shows it to the owner verbatim in its own reply. The owner gives the go in chat and quotes the short code. The agent then records the approval with the owner's quoted reply and the chat source. An approval must quote the prepared digest's short code and is recorded with its chat source; a request field or an approval that does not match the prepared digest is insufficient. This records consent the owner gave in chat; it does not prove that a human gave it, and an agent that ignores the skill could fabricate one. Binding the approval to one digest and consuming it once still prevents reuse for a changed run or a second run.

The approval record stores the prepared digest, source, timestamp, and single-use consumption state. Expensive resume/reconfigure actions require separately prepared and approved invocations.

This design records consent within the trusted local session. It does not provide authentication against a malicious process with equivalent local filesystem privileges.

## 8. Lifecycle, concurrency, and recovery

Expose accepted, running, waiting-for-answer, completed, rejected, failed, and cancelled states. Add an internal/publicly reportable recovery-required condition for uncertain execution after a crash. Receipt means recorded acceptance, not launch success or completion.

Atomically reserve work and consume approval before process creation. Concurrent submissions must not launch the same invocation twice. Record process identity, native run identity, paths, and lifecycle events. A process ID alone is insufficient to identify a recovered job.

A crash between reservation and process recording creates uncertainty. Recovery must reconcile native run records and process identity. If it cannot determine whether launch occurred, mark recovery required and refuse automatic relaunch. Do not promise exactly-once external process execution across that boundary.

Cancellation records a request and uses the adapter's supported stop mechanism. A cancellation receipt is not proof that execution stopped. Report cancelled only after confirmation; preserve partial artifacts. If the engine already completed, return its actual terminal state. Adapters must document unsupported interruption behavior.

Read-only collection may be retried without repeating an experiment. A collection failure must preserve the execution record and native output.

## 9. Limits and repetition

Enforce reserved limits transactionally across the study. Record which limits are runtime-enforced, engine-enforced, or estimates only. Unknown spending is unknown, not zero.

For the Nous milestone, require bounded iteration/run settings supported by the pinned engine. Monetary caps must not be advertised as hard limits unless the adapter can enforce them. Define wall-clock termination only after verifying native stop semantics; otherwise report the limit as advisory.

No automatic expensive retry. A retry or resume needs a reason, an explicit policy allowance, remaining limits, and fresh approval. An equivalent experiment requires an explicit repeat intent linked to the prior attempt. It must not bypass duplicate controls by changing IDs. A negative scientific result is not a retry condition.

*Amendment (owner decision D8, 2026-11-09).* There are no invocation-count caps, per study or per capability. The guards against runaway launches are one owner approval per invocation, no automatic retry or chaining, and the duplicate check on equivalent material. The runtime still counts invocations per study and per capability and shows the counts on every approval card. This replaces the earlier requirements to "enforce configured invocation counts" and to require "a bounded study invocation count".

## 10. Nous adapter

Implement validate, prepare, execute, and collect using the existing Nous reference and collector as starting evidence. Verify actual behavior against the pinned engine before declaring conformance.

The portable request must carry enough semantic information to generate the native campaign, without exposing the native YAML as the public contract. Preserve target-system context, objective, locked parameters, run/iteration limits, model configuration, and experiment commands where required. Reject missing semantics rather than inventing them.

Preserve BLIS representability checks and apparatus locks for BLIS studies. A general adapter must not imply that every question is supported by BLIS. Record exact adapter and engine versions for each invocation; installed-skill metadata must identify the compatible runtime version.

Collection retains native state, ledger, baseline, plan, findings, answer, final-check, and principles artifacts when present. Determine completion from verified native lifecycle evidence, not merely process exit or the existence of an answer file.

## 11. Results and evidence

Represent execution status separately from domain outcome. A completed experiment may support a hypothesis, fail to support it, or be inconclusive. Define the hypothesis being classified; lack of support is not automatically proof of its negation. Use `other` with an explanation for findings that do not fit a hypothesis test.

Runtime failures return explicit failure records and native diagnostic references, never fabricated scientific findings. Partial findings must be labeled partial. If native artifacts cannot justify a classification, record the classification as unavailable with a reason rather than guessing.

Carry the existing answer-contract claims in a versioned findings section or reference a validated answer artifact. Keep `answer.json`'s existing contract unchanged in the initial milestone. Record validator status separately from execution status and outcome. Existing citation checks remain available for qualitative studies.

The final user-facing answer must pass the current validator and retain owner review before writing into `final/`. Validation confirms the implemented evidence checks; it does not certify baseline fairness, causal interpretation, or generalization. Include methods and limitations in the portable result.

## 12. Planning, intake, and other methods

Keep the baseline, falsifiable question, qualitative/quantitative distinction, and prior-study search. Infer known fields from available context and ask only for missing or uncertain information. Make inferred defaults visible before approval.

Change `plan.md` from an approved execution sequence to a provisional method outline. After every capability result, record interpretation, remaining uncertainty, proposed next step or stopping reason, expected information gain, and applicable limits. Plan approval never grants launch approval.

During migration, label legacy methods explicitly. Their existing per-launch gates remain in force, but they must not be described as runtime-enforced. After migration, route Spotlights static, signal-pipeline through stage 04, and telemetry capture through their adapters. Define their result contracts from verified outputs, including no-opportunity and missing-input cases. Never convert an estimated Spotlights impact into a measured claim.

## 13. Optional shared handoff specification

Keep message semantics separate from local storage, process control, and native engine configuration. Publish a versioned local contract with examples and compatibility rules. Hive and Crux may later implement or help revise this contract.

An external sender does not grant local launch authorization. Transport, identity trust, approval delegation, and remote artifact access require a separate integration design. No Hive/Crux service or library is required for local operation or tests.

## 14. Delivery milestones and acceptance

### Milestone A: local runtime with Nous and Spotlights

Deliver contracts, durable state, preparation, approval, duplicate protection, invocation policy (per-run approval, no automatic retry, duplicate checks; D8), evidence integration, and coordinator replanning together with real Nous, Spotlights static, and Spotlights telemetry-driven adapters. Shared runtime work may precede adapter wiring internally, but the milestone is not complete until all three paths work. The telemetry-driven Spotlights path must accept verified existing telemetry inputs; standalone telemetry capture governance is a separate remaining migration item.

Acceptance tests must demonstrate:

- For Nous, Spotlights static, and Spotlights telemetry-driven execution, a valid approved preparation launches through the real adapter; a material mutation is rejected.
- Both Spotlights paths preserve native artifacts, collect evidence and candidate references, distinguish no-opportunity results from execution failures, and never report estimated impact as a measured claim.
- A Spotlights result can lead to stopping, clarification, or a separately justified and approved Nous request. No automatic chaining occurs.
- The telemetry-driven Spotlights path rejects missing or incompatible inputs and cannot execute signal-pipeline stage 05.
- Duplicate delivery and concurrent equivalent submissions do not start duplicate jobs.
- Crashes before and after spawn recover known state or stop for reconciliation without automatic relaunch.
- Missing requirements and unavailable/version-mismatched capabilities fail before launch.
- Cancellation and partial execution are represented truthfully.
- Negative and inconclusive findings remain successful executions when justified.
- Collection and validation failures preserve native evidence and do not rerun the experiment.
- A next step requires fresh justification and approval; stopping requires no follow-on launch.
- Installation into a separate target repository works without Hive/Crux or checkout-specific paths.
- Existing answer validation, tracking modes, and prohibited-action rules remain intact.

Use fake processes and fixture artifacts for failure/concurrency tests. A real paid or expensive integration run requires its own owner approval; mocked tests cannot establish actual native compatibility.

### Milestone B: close the remaining method gaps

Add and verify standalone telemetry capture governance, its approval/limit records and native result mappings, and any remaining coordinator-reference migration. Nous and both Spotlights adapters are already required by Milestone A. Demonstrate result-driven optional transitions between methods and valid stopping behavior. Full migration is complete only when every in-scope expensive launch has a governed path and no skill instruction directs a migrated launch around it.

### Milestone C: nousko adapter and Systems Optimizer handoff

*Proposed amendment (2026-11-09), not yet accepted by the owner.* Source: the tool comparison "Spotlights, Nous, nousko and the Systems Optimizer" (read from code, 9 Nov 2026). Milestone C starts after Milestone B and does not change the scope of Milestones A or B.

**Roles.** Spotlights shows where to look. Nous explains why a mechanism works. nousko certifies which setting of declared knobs is best. The Systems Optimizer measures a change and ships it. AINS answers questions with evidence and does not ship changes. Only nousko becomes a governed AINS capability. The optimizer is a downstream consumer of AINS output.

#### C1. nousko adapter

Add `nousko` as a separate capability, not a mode of the Nous adapter. Its material fields, completion evidence and result mapping differ from classic Nous. The approval card must name the engine being approved.

Use: questions about whether a shipped constant or default still holds, or which setting of a small declared knob space is best. nousko searches only the space the request declares. It does not find new mechanisms. The coordinator must not choose it for open-ended or "where does the time go" questions.

Requirements:

- **Pin.** Do not implement against an unmerged branch. Start C1 only after nousko (agentic-strategy-evolution PR #302) is merged and pinned. Record the engine version in every preparation, as for Nous.
- **Request semantics.** The portable request carries factors and levels, an apply template per factor (CLI flag, environment variable or config patch), a manipulation check and a correctness test per factor, the run and test commands, the known-good baseline, the primary metric, constraints, locked parameters, and the noise estimate. Reject missing semantics rather than inventing them. Native YAML is not the public contract.
- **Material fields.** All of the above, plus ε, δ, the design stages enabled, the optional TPE proposal setting and the opt-in plan/build stage. The native policy hash is recorded in the preparation, and the adapter checks that it matches the material digest at launch.
- **Limits.** The fixed design determines the planned number of runs (screen, fold-over, response surface, confirmation replicates) before launch. Show it on the approval card as engine-enforced only if verified against the pinned engine, and otherwise as an estimate. Resuming an epoch and accepting a `plan-next` proposal each need a separately prepared and approved invocation. `plan-next` output is a proposal to the coordinator, never an automatic next launch.
- **Opt-in build stage.** The build stage asks an LLM to write a new mechanism plus tests. It is material, off by default, and must be listed on the approval card when enabled.
- **Live systems.** If run commands touch anything other than a local simulator or local process, the approval card must say what they touch. Deployment stays prohibited (§3). An adapter that cannot tell whether a command reaches a live system must say so on the card.
- **Outcome mapping.** A certified result supports the stated hypothesis only within the declared space and only if the response model holds. Record ε, δ and that condition as limitations. "Not certified", including a certificate withheld by heteroscedasticity checks, is `inconclusive`, never a negative finding. The deploy-or-fall-back recommendation is evidence, not an action.
- **Collection.** Keep the transitions log, design rows, fitted model diagnostics (lack-of-fit, PRESS, noise checks), certificate, provenance and any recorded mechanism patch. Determine completion from native lifecycle evidence, as for Nous.

Acceptance adds, for nousko: an approved preparation launches through the real adapter; a mutated factor, level, ε/δ or lock is rejected; a policy-hash mismatch blocks launch; not-certified results are reported as inconclusive successful executions; `plan-next` never launches anything by itself; and the planned run count appears on the card.

#### C2. Systems Optimizer handoff

Do not add an optimizer adapter. The optimizer has its own setup, triage, challenger, verifier, gate and `optimize admit` steps. Wrapping it would add a second approval layer and would conflict with the exclusions in §3 (deployment, PR creation, automatic chaining).

Add a handoff step on the model of sim2real prep. It writes files only, launches nothing and needs no runtime invocation:

- Write a bundle under `investigations/<slug>/optimizer-handoff/` with an `external/` folder (selected Spotlights candidates with their citations, still labelled as estimated impact) and an `operator-notes/` folder (Nous principles and nousko certificates, each labelled as a hypothesis for the optimizer to measure, with ε/δ and limitations for certificates).
- Include a manifest with the source study, target repository and commit, and content digests.
- Print, without running, the command that copies the bundle into the optimizer session's `context/external/` and `context/operator-notes/` folders. The owner runs it.
- Never write into an optimizer session, and never run `./optimize` or `optimize admit`.

Acceptance: the handoff writes only under the study, keeps estimated Spotlights impact labelled as estimated, launches no process and passes the existing commit guards.

#### C3. Deferred

- A read-only collector for optimizer diagnostic artifacts (measure-only runs, host profile wait-for graph, stack samples) as evidence for "where does the time go" answers. Deferred until the optimizer's diagnostics are on its main branch and an AINS target needs them. Raise priority if AINS questions come from Certus targets.
- Optimizer → nousko (certifying settings around an admitted code change) belongs on the optimizer side, not in AINS.
- Spotlights `prep-evolve --evolver nous` stays out of scope: it chains capabilities automatically. The governed Spotlights → Nous transition from Milestone A replaces it.

## 15. Verification questions before implementation is finalized

Resolve these through code inspection and focused interface tests, then record the answers in the implementation plan:

- What owner-facing approval mechanism is available in each supported agent host?
- Which exact native states identify completed, stopped, resumable, and incomplete Nous runs?
- Which limits, stop operations, and cost reports are supported by the pinned engines?
- Which native artifacts support each normalized finding and limitation?
- Which material settings depend on environment variables or mutable files outside the campaign?
- Which repository revision and dirty-worktree policy can the adapter enforce reproducibly?

Until the last question is resolved, the conservative default is to reject dirty target state for execution rather than claim that a commit identifies uncommitted inputs. No unresolved answer may be replaced by invented evidence or an undocumented enforcement promise.
