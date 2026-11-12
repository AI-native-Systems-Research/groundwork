"""Environment policy: secret detection and redaction.

Two halves:

1. Secrets (R1). A name pattern for variables that may hold credentials, and
   a scan that finds any such variable's VALUE inside a message or record,
   so contracts.py can refuse it and store/export never persist it (spec §6:
   never put credentials in messages, prepared records, or logs).

2. The environment record of a preparation (R2; plan §1 Q5, decision D9):

   material   the variables an adapter lists in its material-field policy
              (e.g. NOUS_CAMPAIGN_PARENT, SPOTLIGHTS_*), by value, plus the
              sha256 of every file such a variable or the adapter names
              (model-config files). Part of the material digest.
   observed   every ANTHROPIC_* / CLAUDE_* variable. Recorded for the
              record only, NOT material: launch warns, and does not refuse,
              when these differ from what was prepared (env_drift()).
   secrets    variables named by the request (campaign `secrets:`), by
              presence only.

   Any secret-named variable (SECRET_NAME_RE) is recorded as "present" or
   "absent" only -- never by value and never by hash (a hash of a short
   token is guessable). A non-secret value that happens to contain a secret
   value is recorded as "redacted".

A secret value is never echoed: findings name the JSON path and the variable
NAME only. Values shorter than MIN_SECRET_LEN are ignored, because short
values ("1", "true") would match almost any payload.
"""
from __future__ import annotations

import os
import re
from typing import Any, Iterable, Mapping

SECRET_NAME_RE = re.compile(r"(?:^|_)\d*(KEY|TOKEN|SECRET|PASSWORD|AUTH)\d*(?:_|$)", re.IGNORECASE)
MIN_SECRET_LEN = 8


def is_secret_name(name: str) -> bool:
    return bool(SECRET_NAME_RE.search(name))


def secret_values(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """{name: value} for every secret-named variable with a long enough value."""
    env = os.environ if env is None else env
    return {k: v for k, v in env.items()
            if is_secret_name(k) and isinstance(v, str) and len(v) >= MIN_SECRET_LEN}


def _strings(obj: Any, path: str) -> Iterable[tuple[str, str]]:
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield f"{path}.<key>", str(k)
            yield from _strings(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _strings(v, f"{path}[{i}]")


def find_secret_values(obj: Any, env: Mapping[str, str] | None = None) -> list[str]:
    """Paths in obj whose string (or key) contains a secret value.

    Each finding is "<json path>: contains the value of $<NAME>"; the value
    itself is never included.
    """
    secrets = secret_values(env)
    if not secrets:
        return []
    out = []
    for path, s in _strings(obj, "$"):
        for name, value in secrets.items():
            if value in s:
                out.append(f"{path}: contains the value of ${name}")
    return out


def text_contains_secret(text: str, env: Mapping[str, str] | None = None) -> list[str]:
    """Names of secret variables whose value appears in text."""
    return sorted(n for n, v in secret_values(env).items() if v in text)


def redact(text: str, env: Mapping[str, str] | None = None) -> str:
    """Replace every secret value in text with [REDACTED:$NAME] (for logs)."""
    # Longest first, so a value that contains another is redacted whole.
    for name, value in sorted(secret_values(env).items(), key=lambda kv: -len(kv[1])):
        text = text.replace(value, f"[REDACTED:${name}]")
    return text


# --- preparation environment record (R2, D9) -------------------------------

OBSERVED_PREFIXES = ("ANTHROPIC_", "CLAUDE_")
PRESENT, ABSENT, REDACTED = "present", "absent", "redacted"


def _record_value(name: str, env: Mapping[str, str]) -> str | None:
    """How one variable is recorded: None (unset), present, redacted or the value."""
    if name not in env:
        return None
    if is_secret_name(name):
        return PRESENT
    v = env[name]
    if any(sv in v for sv in secret_values(env).values()):
        return REDACTED
    return v


def file_digest(path: str | os.PathLike | None) -> str:
    """'sha256:<hex>' of a file's bytes, or 'absent'. Contents are never stored."""
    import hashlib
    if not path:
        return ABSENT
    p = os.path.expanduser(os.fspath(path))
    try:
        with open(p, "rb") as f:
            h = hashlib.sha256()
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
    except OSError:
        return ABSENT
    return "sha256:" + h.hexdigest()


def env_record(env: Mapping[str, str] | None = None, *,
               material_vars: Iterable[str] = (),
               file_vars: Iterable[str] = (),
               files: Mapping[str, str] | None = None,
               secret_vars: Iterable[str] = (),
               observed_prefixes: Iterable[str] = OBSERVED_PREFIXES) -> dict:
    """The preparation's environment record (see module docstring).

    material_vars   names whose value is material (unset -> null)
    file_vars       subset of names that point at a file; the file's digest
                    is material too (material.files["$NAME"])
    files           {label: path} of other material files (e.g. a
                    model-config file); digest only
    secret_vars     names recorded by presence only (campaign secrets:)

    Returns {"material": {"vars", "files"}, "observed": {...},
             "secrets": {...}}. Only "material" belongs in the digest.
    """
    env = os.environ if env is None else env
    material_vars = sorted(set(material_vars) | set(file_vars))
    mvars = {n: _record_value(n, env) for n in material_vars}
    mfiles = {f"${n}": file_digest(env.get(n)) for n in sorted(set(file_vars))}
    for label, path in sorted((files or {}).items()):
        mfiles[label] = file_digest(path)
    prefixes = tuple(observed_prefixes)
    observed = {n: _record_value(n, env) for n in sorted(env)
                if n.startswith(prefixes) and n not in mvars}
    secrets = {n: (PRESENT if n in env else ABSENT) for n in sorted(set(secret_vars))}
    return {"material": {"vars": mvars, "files": mfiles},
            "observed": observed, "secrets": secrets}


def env_drift(prepared: Mapping, env: Mapping[str, str] | None = None, *,
              exclude: Iterable[str] = (),
              observed_prefixes: Iterable[str] = OBSERVED_PREFIXES) -> list[str]:
    """Warnings for observed (non-material) variables that changed since prepare.

    `prepared` is env_record(...)["observed"]; `exclude` is the material
    variable names (checked as material, not here). Never refuses (D9).
    Messages name the variable only, never a value.
    """
    env = os.environ if env is None else env
    prefixes = tuple(observed_prefixes)
    skip = set(exclude)
    now = {n: _record_value(n, env) for n in env
           if n.startswith(prefixes) and n not in skip}
    out = []
    for n in sorted(set(prepared) | set(now)):
        if n not in now and n in prepared and prepared[n] is not None:
            out.append(f"${n} was set at prepare and is now unset")
        elif n not in prepared:
            out.append(f"${n} is set now but was not at prepare")
        elif prepared[n] != now[n]:
            out.append(f"${n} changed since prepare")
    return out
