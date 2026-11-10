# AINS Local Capability Runtime — v0 Implementation Spec

## 1. Purpose

Build a lightweight local orchestration layer that makes AI-native system components such as **Nous** and **Spotlights** easy for a developer to run and compose from their local coding agent.

The intended user experience is:

> Install the AINS skill, describe what you want to accomplish, and let the skill coordinate the appropriate AI-native capabilities with explicit approval and deterministic execution.

The same capability contracts and message semantics should later be usable inside **Hive/Crux** without redesigning the integrations.

The system should combine:

- LLM reasoning for deciding what should happen next
- deterministic code for validation and execution
- explicit user approval for expensive operations
- typed messages for handoffs between capabilities
- bounded dynamic replanning rather than fixed workflows

---

# 2. Core principles

## 2.1 Install the skill, not the system

The only deliberate setup step for a local developer should eventually be:

> Install the AINS skill.

For the first POC, however, the runtime may detect that Nous or Spotlights are not installed and stop with a clear setup requirement rather than fully automating installation.

Automatic installation/bootstrap remains part of the intended architecture but is not required for the first implementation slice.

---

## 2.2 The skill reasons; the runtime enforces

The main AINS skill acts as the coordinator and planner.

It reasons about:

- the user's goal
- available evidence
- prior capability results
- what capability is appropriate next
- whether more information is required
- whether the goal is already satisfied

The AINS Runtime is deterministic.

It enforces:

- input validation
- capability preconditions
- approvals
- ordering constraints
- budgets
- retry limits
- duplicate prevention
- loop prevention
- version pinning
- execution
- state recording

The runtime must not invent semantic experiment inputs or decide what the user is trying to accomplish.

---

## 2.3 Plan one step ahead

The system must **not choose a complete workflow up front**.

Instead:

```text
User goal
   ↓
Coordinator selects next valid capability
   ↓
User approves exact invocation
   ↓
Runtime executes
   ↓
Capability returns result + artifacts
   ↓
Coordinator re-evaluates current state
   ↓
Choose next capability, ask a question, or stop
```

Planning is incremental and evidence-driven.

A Spotlights result might lead to:

- a Nous experiment
- direct specification work
- further clarification
- no further action

A Nous result might lead to:

- another experiment
- a specification
- implementation work
- stopping because the hypothesis was disproved
- stopping because evidence is inconclusive

Receiving a result must **never automatically trigger the next capability**.

---

# 3. High-level architecture

```text
Developer
   ↓
Local coding agent
   ↓
AINS Coordinator Skill
   ↓
Typed Work Message
   ↓
AINS Runtime
   ↓
Capability Adapter
   ↓
Nous / Spotlights / future capability
   ↓
Native artifacts + execution state
   ↓
Typed Result Message
   ↓
AINS Coordinator Skill
   ↓
Re-plan
```

Later under Hive:

```text
Hive
   ↓
Crux-managed agent
   ↓
Same AINS coordinator logic
   ↓
Same work-message contracts
   ↓
Same runtime / adapter model
   ↓
Same capabilities
```

Hive owns durable work coordination.

Crux owns agent execution and governance.

The AINS layer owns **capability composition semantics and deterministic invocation**.

---

# 4. Main components

## 4.1 AINS Coordinator Skill

The main skill is the user-facing planner.

Responsibilities:

- understand the user's intent
- inspect available repository/environment context
- infer known required fields
- ask only for missing required information
- choose the next capability
- construct typed work messages
- present exact invocation details for approval
- submit approved work to the runtime
- interpret typed results
- inspect native artifacts when necessary
- re-plan after each result
- stop when no further action is justified

It must not:

- execute Nous or Spotlights directly
- silently change approved material inputs
- automatically chain capabilities
- retry expensive capabilities without policy and approval
- create artificial evidence when a capability returns incomplete findings

---

## 4.2 AINS Runtime

The runtime is the deterministic local execution layer.

Responsibilities:

- validate work messages
- validate capability availability
- validate capability preconditions
- validate approval
- enforce retry and repetition limits
- enforce loop protections
- enforce budget constraints
- resolve adapter version
- execute prepared invocation
- collect native artifacts
- store execution records
- return receipts
- return typed results where possible
- distinguish execution failure from valid domain outcomes

The runtime should be usable independently of any specific LLM agent.

---

## 4.3 Capability Adapters

Each external component is integrated through an adapter.

Initial adapters:

- Nous
- Spotlights

Adapters are deliberately thin.

They should:

1. validate capability-specific requirements
2. translate portable work messages into native capability inputs
3. prepare the invocation
4. execute through the runtime
5. collect native artifacts
6. translate native outputs into portable result messages

Adapters must not perform orchestration or decide what should happen next.

---

# 5. Portable work-message model

The portable message contract is the primary interface between the coordinator and capabilities.

Large artifacts remain outside the message and are referenced by stable artifact references and content fingerprints.

Every message uses a small shared envelope.

## 5.1 Common envelope

```yaml
message_type: <type + version>
message_id: <globally unique message id>

work_id: <stable logical work id>
revision: <explicit revision number>

parent_work_id: <optional>
reply_to_message_id: <optional>

sender: <configured identity>
receiver: <configured identity>

target:
  project: <project identifier>
  repository: <optional>
  commit: <optional full commit sha>

requested_action: <semantic action>

artifact_refs: []

budget: <optional>
created_at: <timestamp>
deadline: <optional>

applicable_policy: <optional>
```

The runtime must treat `message_id` as idempotent.

Receiving the same message twice must not create duplicate execution.

---

# 6. Message types

Initial portable message types are:

```text
experiment.request.v1
experiment.result.v1

work.receipt.v1
work.progress.v1

clarification.request.v1
clarification.answer.v1

work.cancel.request.v1
work.cancel.result.v1
```

Future pipeline messages may include:

```text
specification.request.v1
specification.result.v1

implementation.request.v1
implementation.result.v1
```

Those are not required for the first POC.

---

# 7. Explicit work states

Use explicit work states such as:

```text
accepted
running
waiting_for_answer
completed
rejected
failed
cancelled
```

These states refer to execution/work lifecycle.

They must not be confused with experiment outcome.

Example:

```yaml
status: completed

outcome:
  classification: unsupported
```

means:

> The experiment successfully completed and disproved the hypothesis.

That is not a failed run.

---

# 8. Receipt vs result

The implementation must maintain a hard distinction between:

## Delivery receipt

Example:

```text
Accepted and saved.
```

or

```text
Rejected because the target commit is missing.
```

## Work result

Example:

```text
The experiment completed and found no improvement.
```

or:

```text
The specification was created.
```

Acceptance does not imply completion.

---

# 9. Dynamic replanning rules

After every completed capability result, the coordinator must reassess the current state.

It may:

- propose another capability
- ask the user for clarification
- stop
- propose a retry
- propose a different experiment
- route work toward specification or implementation

Before proposing another capability, the coordinator/runtime must verify:

- the new step is semantically justified
- prerequisites are satisfied
- it is expected to add materially new information
- it is not identical to a prior invocation
- repetition limits are not exceeded
- budget remains
- new approval is obtained if required

---

# 10. Anti-loop rules

The runtime should enforce hard protections.

At minimum:

- identical approved requests cannot execute twice
- the same capability cannot repeat with materially identical input
- retries require explicit retry policy
- expensive repeated execution requires re-approval
- capability invocation count is bounded
- a result must produce materially new state before an equivalent capability can be run again

The coordinator may recommend repetition, but the runtime decides whether it is valid.

---

# 11. Approval model

Every expensive Nous or Spotlights invocation requires explicit user approval in v0.

Approval binds to the **fully resolved material invocation**.

The approval view should show:

- capability
- capability version
- research question / opportunity
- target repository
- commit
- task type
- target system
- objective
- locked parameters
- execution limits
- budget
- any defaults filled automatically

Material changes require new approval.

Material fields include:

- research question
- target repository
- commit
- task type
- objective
- locked parameters
- execution limits
- capability version

Non-material metadata does not require re-approval:

- message ID
- run ID
- timestamps
- log paths
- generated artifact path
- internal execution identifiers

---

# 12. Intake model

The coordinator should not run a rigid questionnaire.

Use this rule:

> Infer aggressively, ask selectively, confirm before execution.

The coordinator should:

1. inspect the user's request
2. inspect repository/environment context
3. populate what it can
4. identify missing required semantic inputs
5. ask only for those fields
6. apply defaults
7. show the resolved request
8. request approval

---

# 13. Nous experiment request

## 13.1 Required semantic inputs

Based on the current Nous v2 campaign example, the required inputs for the local POC are:

- `research_question`
- `task_type`
- `target_system.name`
- `target_system.description`

The coordinator should also resolve:

- target repository
- repo path
- exact commit

Other fields can be defaulted or user-overridden.

---

## 13.2 Portable request contract

```yaml
message_type: experiment.request.v1
message_id: <generated>
work_id: <stable work id>
revision: 1

sender: ains.coordinator
receiver: nous

target:
  project: <project>
  repo_path: <resolved path>
  commit: <full sha>

request:
  research_question: <required>

  task_type: <required>
  # debug | verify | optimize | policy | explain | other

  target_system:
    name: <required>
    description: <required>

  objective:
    metric: <optional>
    direction: <optional>

  locked_parameters: <optional>

  execution:
    max_iterations: <defaulted, user-overridable>

context:
  evidence_refs: []

policy:
  budget: <optional>
  applicable_policy: <optional>
```

---

# 14. Mapping to native Nous campaign

The portable contract should not simply mirror Nous's current YAML schema.

The Nous adapter translates:

```text
experiment.request.v1
        ↓
Nous adapter
        ↓
campaign.yaml
        ↓
Nous
```

Current known example fields include:

```yaml
research_question:
run_id:
max_iterations:
task_type:

target_system:
  name:
  description:
  repo_path:

objective:
  metric:
  direction:

locked_parameters:
```

The adapter owns this translation.

If Nous changes its YAML format later, the portable message contract should not necessarily change.

---

# 15. Nous adapter contract

The Nous adapter exposes four logical operations:

```text
validate
prepare
execute
collect
```

## Validate

Verify that the request can be converted into a valid Nous run.

It must reject missing semantic requirements rather than inventing them.

## Prepare

Produce a prepared invocation containing:

```text
prepared invocation
- capability version
- generated campaign file
- working directory
- exact command
- environment
- expected artifact locations
```

## Execute

Run the exact prepared invocation.

No semantic changes may occur after approval.

## Collect

Collect:

- native Nous artifacts
- logs
- execution metadata
- domain findings if available

Then produce a portable result message.

---

# 16. Nous result contract

```yaml
message_type: experiment.result.v1
message_id: <generated>

work_id: <same work id>
revision: <request revision>

sender: nous
receiver: ains.coordinator

reply_to_message_id: <request message id>

status: completed

outcome:
  classification: <supported | unsupported | inconclusive | other>
  summary: <short finding summary>

findings:
  measurements: <structured or artifact refs>
  methods: <summary or artifact ref>
  limitations: <structured or artifact refs>
  evidence_refs: []

artifacts:
  campaign: <artifact ref>
  results: []
  logs: []

execution:
  capability_version: <version>
  started_at: <timestamp>
  completed_at: <timestamp>
```

Negative and inconclusive findings must remain first-class valid results.

---

# 17. Runtime failure semantics

Execution failure must remain separate from experiment outcome.

Examples:

```yaml
execution_status: failed
reason: benchmark_command_failed
```

or:

```yaml
execution_status: failed
reason: invalid_campaign
```

A runtime failure must not generate a fake `experiment.result.v1`.

The coordinator decides what happens next.

---

# 18. Local state

For the POC, state should be:

> local and project-scoped.

Persist:

```text
work IDs
message IDs
revisions
approval records
receipts
execution records
capability versions
input fingerprints
artifact references
result messages
retry counts
budget usage
timestamps
```

Do not yet create generalized domain objects such as:

```text
Opportunity
Finding
ValidatedStrategy
EngineeringProposal
```

Those abstractions can emerge later from real integrations.

---

# 19. Versioning

The installed skill should eventually pin a runtime version.

Runtime and capability versions must be recorded for every invocation.

The POC may begin with manually available Nous/Spotlights installations, but adapter behavior should assume explicit versions.

---

# 20. First-run behavior for POC

For the first POC:

1. detect whether Nous exists
2. verify it is runnable
3. verify target repository context
4. if missing, stop with clear setup instructions
5. do not silently install or mutate large dependencies yet

Future versions can automatically bootstrap:

- AINS runtime
- Nous
- Spotlights
- isolated environments
- caches

---

# 21. Spotlights integration model

Spotlights is conceptually an **opportunity-discovery capability**.

Its typical result may become input to Nous.

Example future flow:

```text
User goal
   ↓
Spotlights request
   ↓
Spotlights result
   ↓
Coordinator replans
   ├─ experimentable opportunity → Nous
   ├─ directly actionable → skip Nous
   ├─ insufficient signal → clarification / stop
   └─ no worthwhile opportunity → stop
```

Spotlights must not automatically trigger Nous.

---

# 22. Spotlights portable contract direction

The likely portable model is:

```text
opportunity.request.v1
opportunity.result.v1
```

Potential request semantics may include:

- target system
- repository / revision
- investigation scope
- objective / operator priorities
- evidence context
- limits / budget

Potential result semantics may include:

- discovered opportunities
- supporting evidence
- confidence / limitations
- localization
- likely impact
- recommended investigation questions
- artifact references

However, this is **not yet locked**.

---

# 23. Known unresolved gaps

These should be tracked explicitly during implementation.

## 23.1 Nous invocation details

Still unresolved:

- exact Nous v2 CLI invocation
- required environment setup
- how the campaign file is supplied
- process lifecycle
- whether Nous supports native resume
- whether Nous supports native retry
- how status is reported
- how partial execution is represented

This affects the adapter's `prepare`, `execute`, and `collect` implementation.

It does not change the architecture.

---

## 23.2 Nous output artifacts

Still unresolved:

- exact output directory structure
- canonical result files
- which artifact contains the final experiment finding
- where measurements live
- how methods are recorded
- how limitations are represented
- whether there is already an explicit supported/unsupported/inconclusive classification
- how incomplete campaigns are distinguished from completed experiments
- whether provenance is already captured
- how run IDs map to artifact locations

Until verified, the exact mapping from native Nous output to `experiment.result.v1` remains provisional.

The implementation should preserve all native Nous artifacts even before the normalized mapping is complete.

---

## 23.3 Spotlights invocation contract

Still unresolved:

- exact local invocation method
- required input fields
- whether it consumes a repository directly
- whether it consumes metrics/logs/artifacts
- whether it requires a separate campaign/config format
- execution lifecycle
- resume/retry behavior

---

## 23.4 Spotlights output contract

This is currently the largest semantic gap.

We still need to determine:

- exact output artifact structure
- whether Spotlights already produces a stable opportunity object
- whether opportunities have IDs
- whether it records evidence and localization
- whether it records confidence or uncertainty
- whether it emits recommended research questions
- whether it distinguishes opportunity discovery from implementation recommendation
- how negative/no-opportunity results are represented
- what minimum subset can reliably populate an `experiment.request.v1`

Until that is known, `opportunity.result.v1` should remain a proposed contract rather than a locked implementation requirement.

---

# 24. POC scope

The first POC should implement **Nous only**.

Required components:

```text
AINS coordinator skill
local runtime
message envelope
experiment.request.v1
approval handling
local state
Nous adapter
execution record
experiment.result.v1
basic anti-loop/idempotency logic
```

Spotlights should be added only after the Nous path works cleanly.

---

# 25. POC end-to-end flow

```text
User asks for an experiment
        ↓
Coordinator extracts intent
        ↓
Coordinator inspects repo/environment
        ↓
Coordinator asks for missing required fields
        ↓
Coordinator creates experiment.request.v1
        ↓
Defaults resolved
        ↓
Exact invocation shown to user
        ↓
User approves
        ↓
Runtime validates:
  message
  approval
  capability
  preconditions
  duplicate state
  budget
        ↓
Nous adapter validates
        ↓
Nous adapter prepares campaign.yaml
        ↓
Runtime executes Nous
        ↓
Native artifacts preserved
        ↓
Adapter collects outputs
        ↓
experiment.result.v1 returned
        ↓
Coordinator interprets result
        ↓
Coordinator replans or stops
```

---

# 26. POC success criteria

The first POC is complete when a developer can:

1. invoke the AINS skill from a local agent session
2. describe a Nous experiment conversationally
3. have repository context discovered automatically
4. provide only missing required semantic inputs
5. see the resolved experiment request
6. approve the exact run
7. execute Nous through the deterministic runtime
8. preserve Nous's native artifacts
9. receive either:
   - a valid experiment result, including negative or inconclusive findings
   - or a clearly distinguished runtime failure
10. have duplicate message IDs prevented from starting duplicate work
11. have the coordinator consume the result and decide what happens next
12. demonstrate that no fixed workflow or automatic chaining is required

---

# 27. Explicit non-goals for v0

Do not build:

- a general-purpose workflow engine
- arbitrary DAG execution
- distributed queues
- multi-user coordination
- automatic GitHub issues
- PR creation
- Spek integration
- Hive integration
- fixed Spotlights → Nous pipelines
- universal engineering-domain objects
- generalized evidence ontology
- automatic capability retries
- autonomous expensive loops
- sophisticated policy language
- production authentication/authorization infrastructure

---

# 28. Suggested implementation order

## Phase 1 — Runtime skeleton

Implement:

```text
message envelope
local state
message idempotency
approval record
execution record
adapter interface
```

## Phase 2 — Nous adapter shell

Implement:

```text
validate()
prepare()
execute()
collect()
```

Stub unresolved Nous-specific behavior where necessary.

## Phase 3 — Coordinator skill

Implement:

- conversational intake
- repo inspection
- missing-field questions
- defaults
- approval presentation
- result interpretation
- re-planning rules

## Phase 4 — Real Nous wiring

Resolve:

- actual CLI
- artifacts
- completion semantics
- result mapping

Replace adapter stubs with real behavior.

## Phase 5 — Spotlights research

Document:

- invocation
- inputs
- outputs
- native artifacts
- opportunity representation

Then define:

```text
opportunity.request.v1
opportunity.result.v1
```

## Phase 6 — Dynamic composition POC

Prove:

```text
Spotlights
    ↓
result
    ↓
re-plan
    ↓
optional Nous
```

without fixed workflow chaining.

---

# 29. Architectural invariant

The most important invariant for implementation is:

> **Work messages are the portable composition contract. Runtime records are the execution contract. Native artifacts remain authoritative capability evidence.**

And the planning rule is:

> **Plan one step ahead, execute deterministically, then re-plan from evidence.**

That is the boundary the POC should preserve even if the internals of Nous, Spotlights, Hive, or Crux evolve.