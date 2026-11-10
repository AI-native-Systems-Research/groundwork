"""AINS local runtime: contracts, durable state and (later) governed launches.

Version constants live here so doctor.py can read them as text without
importing the package (doctor stays stdlib-only and import-free of this).
"""
from __future__ import annotations

RUNTIME_VERSION = "0.1.0"

# Message schema versions this runtime accepts, by message type. A message's
# `schema` field is "ains.<type>/v<N>"; any other <N> is rejected.
SCHEMA_VERSIONS = {
    "experiment-request": (1,),
    "receipt": (1,),
    "progress": (1,),
    "clarification": (1,),
    "cancel": (1,),
    "experiment-result": (1,),
}

# SQLite store schema version (store.py). A DB with a higher version is
# refused; a lower version is migrated through store.MIGRATIONS.
STORE_SCHEMA_VERSION = 1

# Version tag of the reviewable JSON export written into $STUDY/runtime/.
EXPORT_SCHEMA = "ains.runtime-export/v1"
