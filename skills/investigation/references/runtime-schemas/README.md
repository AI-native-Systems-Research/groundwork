# Runtime message schemas (v1)

JSON Schema documents for the AINS local runtime's messages
(`scripts/runtime.py`). They are **documentation**: the runtime validates with
`scripts/ains_runtime/contracts.py` (standard library only). If a schema here
and the code disagree, the code is authoritative and the schema is a bug.

| File | Message | Who writes it |
|---|---|---|
| `envelope.v1.json` | shared envelope + artifact ref | — |
| `experiment-request.v1.json` | `ains.experiment-request/v1` | coordinator (`submit`) |
| `clarification.v1.json` | `ains.clarification/v1` | coordinator (`submit`) |
| `cancel.v1.json` | `ains.cancel/v1` | coordinator (`submit`) |
| `receipt.v1.json` | `ains.receipt/v1` | runtime only |
| `progress.v1.json` | `ains.progress/v1` | runtime only |
| `experiment-result.v1.json` | `ains.experiment-result/v1` | runtime only |

`examples/` holds one valid message per type; the unit tests check every
example against `contracts.py`.

Compatibility: a message whose `schema` names a version the runtime does not
support is rejected, never coerced. Unknown fields are rejected. Required
fields are never inferred. A receipt means the message was recorded, not that
anything was launched or finished. Accepting a request grants no launch.
