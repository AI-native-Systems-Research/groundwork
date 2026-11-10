"""Preparation (spec §7 steps 1-3) and pre-launch revalidation (step 6).

prepare(store, work_id, ...) runs, in order:

  1. load the work's current experiment-request from THIS DB
  2. adapter.check_semantics + adapter.validate (native requirements)
  3. target checks (gitstate): repo name and HEAD match the request's
     target, and the tree is clean (D4)
  4. adapter.prepare -> the exact invocation
  5. build the material record and its digest (plan §4.2)
  6. refuse if any secret value appears in the record, inputs or card
  7. write the generated inputs + card.md under
     .runtime/prepared/<prep-id>/ and make them read-only
  8. store.insert_preparation (the DB is the authority; the directory is
     removed again if the insert fails)

revalidate(store, prep_id, ...) is what R3's `launch` calls before
store.reserve_invocation(..., expected_digest=result.material_digest). It
re-checks, without writing anything: the stored record still digests to its
recorded digest; the approval exists, matches and is unused; the work has no
newer revision; runtime/adapter/engine versions are unchanged; the prepared
input files are byte-identical; HEAD is the prepared commit and the tree is
clean; the material env is unchanged; adapter.validate still passes; and the
material rebuilt from the current state digests the same. Changes to the
observed ANTHROPIC_*/CLAUDE_* variables are warnings only (D9).

The material record (what the digest covers):

  runtime_version, adapter{capability, version}, versions (adapter.versions)
  target{repo_name, commit, clean, clean_policy}
  request{question, task_type, objective, baseline, hypothesis, locks,
          limits, semantics (adapter MATERIAL_FIELDS only)}
  adapter_material, invocation{argv, cwd, env_set, inputs{relpath: digest},
  expected_artifacts}, env (envpolicy material part), limits, defaults

Not material, recorded beside it: ids (prep, work, message, revision),
next_step / repeat fields, the observed env, secret presence, and the
values of non-material fields (prep dir, run ids, output paths), which are
replaced by "{name}" placeholders inside the material copy. So two
preparations of equivalent requests get the same digest, and the store's
duplicate index can see them (spec §6).
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from . import RUNTIME_VERSION
from . import adapters as A
from . import gitstate
from .canonical import material_digest, sha256_hex
from .envpolicy import env_drift, env_record, find_secret_values, text_contains_secret
from .store import Store, StoreError

PREP_SCHEMA = "ains.preparation/v1"
PREPARED_DIR = "prepared"
CARD_NAME = "card.md"
PREP_DIR_PLACEHOLDER = "prep_dir"


class PrepareError(Exception):
    """Preparation refused. .reasons lists why (no secret values)."""

    def __init__(self, reasons: list[str] | str):
        self.reasons = [reasons] if isinstance(reasons, str) else list(reasons)
        super().__init__("; ".join(self.reasons))


def new_prep_id() -> str:
    return "prep-" + secrets.token_hex(6)


def runtime_dir(store: Store) -> Path:
    return store.path.parent


def prepared_root(store: Store) -> Path:
    return runtime_dir(store) / PREPARED_DIR


def prep_dir(store: Store, prep_id: str) -> Path:
    return prepared_root(store) / prep_id


def file_digest_bytes(data: bytes) -> str:
    return "sha256:" + sha256_hex(data)


# --- templating of non-material values -------------------------------------

# A value is replaced only where it stands as a whole token: not preceded
# or followed by a name character. So a short run id like "r1" is not
# replaced inside an unrelated temp path such as "/tmp/abr1x" (which would
# change the digest and hide a duplicate).
_TOKEN_CHARS = r"A-Za-z0-9_.\-"


def _subs(non_material: Mapping[str, str]) -> list[tuple["re.Pattern[str]", str]]:
    # Longest value first, so a value containing another is replaced whole.
    pairs = sorted(((v, "{" + k + "}") for k, v in non_material.items() if v),
                   key=lambda kv: -len(kv[0]))
    return [(re.compile(rf"(?<![{_TOKEN_CHARS}]){re.escape(v)}(?![{_TOKEN_CHARS}])"), ph)
            for v, ph in pairs]


def template(obj: Any, non_material: Mapping[str, str]) -> Any:
    subs = _subs(non_material)

    def t(x):
        if isinstance(x, str):
            for rx, ph in subs:
                x = rx.sub(lambda _m, ph=ph: ph, x)
            return x
        if isinstance(x, dict):
            return {t(k): t(v) for k, v in x.items()}
        if isinstance(x, (list, tuple)):
            return [t(v) for v in x]
        return x
    return t(obj)


def template_bytes(data: bytes, non_material: Mapping[str, str]) -> bytes:
    text = data.decode("utf-8", errors="surrogateescape")
    for rx, ph in _subs(non_material):
        text = rx.sub(lambda _m, ph=ph: ph, text)
    return text.encode("utf-8", errors="surrogateescape")


# --- building the record ----------------------------------------------------

def _request_for_work(store: Store, work_id: str) -> tuple[dict, dict]:
    work = store.get_work(work_id)
    if work is None:
        raise PrepareError(f"no work {work_id!r} in this runtime DB")
    msg = store.get_message(work["request_message_id"])
    if msg is None or msg["type"] != "experiment-request":
        raise PrepareError(f"work {work_id!r} has no recorded experiment-request")
    return work, msg


def _check_rel(relpath: str) -> None:
    p = Path(relpath)
    if p.is_absolute() or ".." in p.parts or not p.parts or relpath == CARD_NAME:
        raise PrepareError(f"adapter input path {relpath!r} is not a plain relative path")


def _target_checks(target: Path, request: dict) -> tuple[gitstate.GitState, list[str]]:
    try:
        gs = gitstate.snapshot(target)
    except gitstate.GitStateError as e:
        raise PrepareError(str(e)) from e
    import _config  # gitstate put scripts/ on sys.path
    reasons = []
    want_repo = request["target"]["repo_name"]
    have_repo = _config.repo_name(gs.toplevel)
    if have_repo != want_repo:
        reasons.append(f"target repo is {have_repo!r}, the request is for {want_repo!r}")
    reasons += gitstate.problems(gs, expected_commit=request["target"]["commit"])
    return gs, reasons


def build_material(adapter: A.Adapter, request: dict, prepared: A.Prepared,
                   gs: gitstate.GitState, ctx: A.AdapterContext,
                   non_material: Mapping[str, str]) -> tuple[dict, dict, dict]:
    """(material, env_rec, input_digests). Pure: reads env and files only."""
    body = request["body"]
    env_rec = env_record(ctx.env, material_vars=prepared.material_vars,
                         file_vars=prepared.file_vars, files=prepared.material_files,
                         secret_vars=prepared.secret_vars)
    inputs = {rel: file_digest_bytes(data) for rel, data in sorted(prepared.files.items())}
    inputs_material = {rel: file_digest_bytes(template_bytes(data, non_material))
                       for rel, data in sorted(prepared.files.items())}
    cwd = prepared.cwd or str(ctx.target)
    material = {
        "runtime_version": RUNTIME_VERSION,
        "adapter": {"capability": adapter.capability, "version": adapter.version},
        "versions": adapter.versions(ctx),
        "target": {"repo_name": request["target"]["repo_name"], **gs.material()},
        "request": {
            "question": body["question"], "task_type": body["task_type"],
            "objective": body["objective"], "baseline": body.get("baseline"),
            "hypothesis": body.get("hypothesis"), "locks": body["locks"],
            "limits": body["limits"],
            "semantics": adapter.material_semantics(request),
        },
        "adapter_material": template(prepared.material, non_material),
        "invocation": template({
            "argv": list(prepared.argv), "cwd": cwd,
            "env_set": dict(prepared.env_set),
            "expected_artifacts": list(prepared.expected_artifacts),
        }, non_material),
        "inputs": inputs_material,
        "env": env_rec["material"],
        "limits": list(prepared.limits),
        "defaults": list(prepared.defaults),
    }
    return material, env_rec, inputs


def _context(store: Store, prep_id: str, target: Path, env: Mapping[str, str],
             repo_name: str, work_id: str) -> A.AdapterContext:
    return A.AdapterContext(target=target, runs_dir=runtime_dir(store).parent,
                            prep_dir=prep_dir(store, prep_id), env=env,
                            repo_name=repo_name, work_id=work_id)


@dataclass
class PrepareResult:
    prep_id: str
    material_digest: str
    short_code: str
    card: str
    card_path: Path
    record: dict


def _make_read_only(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for f in filenames:
            os.chmod(os.path.join(dirpath, f), stat.S_IRUSR | stat.S_IRGRP)
        os.chmod(dirpath, stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)


def make_writable(root: Path) -> None:
    """Undo _make_read_only (cleanup of a failed prepare; tests)."""
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        os.chmod(dirpath, stat.S_IRWXU)
        for f in filenames:
            os.chmod(os.path.join(dirpath, f), stat.S_IRUSR | stat.S_IWUSR)


def next_step_problems(store: Store, request: dict) -> list[str]:
    """Plan §4.1 / spec §14: once this study has a result (any invocation
    that reached a terminal execution state), every further request must
    carry a next_step justification. contracts.py already checks the block's
    shape; whether the step is worthwhile stays the coordinator's and the
    owner's judgement (spec §4)."""
    if request["body"].get("next_step"):
        return []
    done = [i["inv_id"] for i in store.list_invocations()
            if i["state"] in ("completed", "failed", "cancelled")]
    if not done:
        return []
    return [f"body.next_step: required, this study already has a result "
            f"({', '.join(done[:3])}{' ...' if len(done) > 3 else ''}); copy the "
            "interpretation, remaining uncertainty, expected information gain and "
            "rationale from plan.md's step log (or stop: stopping needs no launch)"]


def prepare(store: Store, work_id: str, *, target: str | os.PathLike,
            env: Mapping[str, str] | None = None, prep_id: str | None = None,
            approve_hint: str = "runtime.py") -> PrepareResult:
    """Prepare the work's current request. Raises PrepareError with reasons."""
    from .approval import render_card  # card layout lives with approval
    env = dict(os.environ if env is None else env)
    prep_id = prep_id or new_prep_id()
    work, msg = _request_for_work(store, work_id)
    request = msg["envelope"]
    capability = request["body"]["capability"]
    try:
        adapter = A.get(capability)
    except A.AdapterError as e:
        raise PrepareError(str(e)) from e

    gs, reasons = _target_checks(Path(target), request)
    ctx = _context(store, prep_id, gs.toplevel, env, request["target"]["repo_name"],
                   work_id)
    reasons = adapter.check_semantics(request) + adapter.validate(request, ctx) + reasons
    reasons += next_step_problems(store, request)
    if reasons:
        raise PrepareError(reasons)

    prepared = adapter.prepare(request, ctx)
    for rel in prepared.files:
        _check_rel(rel)
    non_material = {PREP_DIR_PLACEHOLDER: str(ctx.prep_dir), **prepared.non_material}
    material, env_rec, inputs = build_material(adapter, request, prepared, gs, ctx,
                                               non_material)
    mdig = material_digest(material)
    record = {
        "schema": PREP_SCHEMA,
        "prep_id": prep_id, "work_id": work_id, "revision": work["revision"],
        "request_message_id": msg["message_id"],
        "request_payload_digest": msg["payload_digest"],
        "capability": capability,
        "material": material,
        "material_digest": mdig,
        "non_material": dict(non_material),
        "target_path": str(gs.toplevel),
        "argv": list(prepared.argv),
        "cwd": prepared.cwd or str(gs.toplevel),
        "env_set": dict(prepared.env_set),
        "env_policy": {"material_vars": sorted(prepared.material_vars),
                       "file_vars": sorted(prepared.file_vars),
                       "material_files": dict(prepared.material_files),
                       "secret_vars": sorted(prepared.secret_vars)},
        "env_observed": env_rec["observed"],
        "env_secrets": env_rec["secrets"],
        "inputs": [{"kind": "prepared-input", "location": rel, "digest": d,
                    "provisional": False} for rel, d in inputs.items()],
        "expected_artifacts": list(prepared.expected_artifacts),
        "native_run_id": prepared.native_run_id,
        "next_step": request["body"].get("next_step"),
        "repeat_of": request["body"].get("repeat_of"),
        "repeat_reason": request["body"].get("repeat_reason"),
    }
    counts = {"study": store.count_invocations(),
              "capability": store.count_invocations(capability)}
    card = render_card(record, prepared, counts, approve_hint=approve_hint)

    leaks = find_secret_values(record, env)
    leaks += [f"input {rel}: contains the value of ${n}"
              for rel, data in prepared.files.items()
              for n in text_contains_secret(data.decode("utf-8", "replace"), env)]
    leaks += [f"approval card: contains the value of ${n}"
              for n in text_contains_secret(card, env)]
    if leaks:
        raise PrepareError(["preparation refused, secret value found: " + "; ".join(leaks)])

    pdir = ctx.prep_dir
    try:
        pdir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise PrepareError(f"{pdir} already exists") from None
    try:
        for rel, data in prepared.files.items():
            p = pdir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        (pdir / CARD_NAME).write_text(card, encoding="utf-8")
        _make_read_only(pdir)
        store.insert_preparation(prep_id, work_id, record, env=env)
    except BaseException:
        make_writable(pdir)
        shutil.rmtree(pdir, ignore_errors=True)
        raise
    from .canonical import short_code
    return PrepareResult(prep_id, mdig, short_code(mdig), card, pdir / CARD_NAME, record)


# --- revalidation before launch ---------------------------------------------

@dataclass
class Revalidation:
    prep_id: str
    material_digest: str | None
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def revalidate(store: Store, prep_id: str, *, env: Mapping[str, str] | None = None,
               require_approval: bool = True) -> Revalidation:
    """Pre-launch checks (module docstring). Writes nothing.

    R3's launch: rv = revalidate(...); refuse unless rv.ok; then
    store.reserve_invocation(inv_id, prep_id, expected_digest=rv.material_digest)
    (which re-checks and consumes the approval atomically).
    """
    env = dict(os.environ if env is None else env)
    p = store.get_preparation(prep_id)
    if p is None:
        return Revalidation(prep_id, None, [f"no preparation {prep_id!r} in this "
                                            "runtime DB; exported records grant nothing"])
    rec = p["record"]
    mdig = p["material_digest"]
    rv = Revalidation(prep_id, mdig)
    bad = rv.problems.append
    mat = rec.get("material", {})
    if material_digest(mat) != mdig or rec.get("material_digest") != mdig:
        bad("stored preparation record does not match its material digest")

    if require_approval:
        a = store.get_approval(prep_id)
        if a is None:
            bad(f"preparation {prep_id!r} has no approval")
        elif a["material_digest"] != mdig:
            bad("approval is bound to a different material digest")
        elif a["consumed_at"] is not None:
            bad(f"approval for {prep_id!r} was already used")
    if store.get_invocation_for_prep(prep_id) is not None:
        bad(f"preparation {prep_id!r} already has an invocation")

    work = store.get_work(p["work_id"])
    if work is None or work["revision"] != p["revision"]:
        bad("the work has a newer request revision than this preparation")
    msg = store.get_message(rec.get("request_message_id", ""))
    if msg is None or msg["payload_digest"] != rec.get("request_payload_digest"):
        bad("the request this preparation was built from is not in this DB")
        return rv
    request = msg["envelope"]

    if mat.get("runtime_version") != RUNTIME_VERSION:
        bad(f"runtime version changed: prepared {mat.get('runtime_version')}, "
            f"now {RUNTIME_VERSION}")
    try:
        adapter = A.get(rec["capability"])
    except A.AdapterError as e:
        bad(str(e))
        return rv
    if adapter.version != mat.get("adapter", {}).get("version"):
        bad(f"adapter version changed: prepared {mat.get('adapter', {}).get('version')}, "
            f"now {adapter.version}")

    target = Path(rec["target_path"])
    ctx = _context(store, prep_id, target, env, request["target"]["repo_name"],
                   p["work_id"])
    try:
        vers = adapter.versions(ctx)
    except Exception as e:  # an unavailable engine must block, not crash launch
        vers = None
        bad(f"versions unavailable: {e}")
    if vers is not None and vers != mat.get("versions"):
        for k in sorted(set(vers) | set(mat.get("versions") or {})):
            if vers.get(k) != (mat.get("versions") or {}).get(k):
                bad(f"version drift: {k} prepared {(mat.get('versions') or {}).get(k)!r}, "
                    f"now {vers.get(k)!r}")

    pdir = ctx.prep_dir
    for ref in rec.get("inputs", []):
        f = pdir / ref["location"]
        try:
            d = file_digest_bytes(f.read_bytes())
        except OSError:
            bad(f"prepared input {ref['location']} is missing")
            continue
        if d != ref["digest"]:
            bad(f"prepared input {ref['location']} changed since preparation")
        elif os.access(f, os.W_OK):
            rv.warnings.append(f"prepared input {ref['location']} is writable again")

    try:
        gs = gitstate.snapshot(target)
    except gitstate.GitStateError as e:
        bad(str(e))
        return rv
    rv.problems += gitstate.problems(gs, expected_commit=mat.get("target", {}).get("commit"))

    pol = rec.get("env_policy", {})
    now_env = env_record(env, material_vars=pol.get("material_vars", ()),
                         file_vars=pol.get("file_vars", ()),
                         files=pol.get("material_files", {}),
                         secret_vars=pol.get("secret_vars", ()))
    if now_env["material"] != mat.get("env"):
        for kind in ("vars", "files"):
            was, is_ = (mat.get("env") or {}).get(kind, {}), now_env["material"][kind]
            for n in sorted(set(was) | set(is_)):
                if was.get(n) != is_.get(n):
                    bad(f"material environment changed: {n}")
    for n, state in now_env["secrets"].items():
        if state != rec.get("env_secrets", {}).get(n):
            bad(f"secret ${n} is now {state}")
    rv.warnings += env_drift(rec.get("env_observed", {}), env,
                             exclude=pol.get("material_vars", ()))

    try:
        rv.problems += adapter.check_semantics(request) + adapter.validate(request, ctx)
        if rv.ok:
            prepared = adapter.prepare(request, ctx)
            nm = {**rec.get("non_material", {}), **prepared.non_material,
                  PREP_DIR_PLACEHOLDER: str(pdir)}
            again, _, _ = build_material(adapter, request, prepared, gs, ctx, nm)
            if material_digest(again) != mdig:
                bad("material rebuilt from the current state differs from the "
                    "approved preparation; prepare again")
    except Exception as e:
        bad(f"adapter revalidation failed: {e}")
    return rv
