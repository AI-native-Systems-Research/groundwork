#!/usr/bin/env python3
"""Validate a study's answer.json against the groundwork answer contract.

The contract (skills/investigation/references/answer-contract.md; the
mlsys study's contract, also
adopted by Nous v2's answer.schema.json) allows exactly four top-level
keys: answer (non-empty string), deliverable (null or an existing path),
repro (ONE shell command that regenerates the headline numbers), and
claims — a non-empty list of {id, metric, value, condition, evidence}
objects, each with exactly those five keys.

The iron rule — the load-bearing check — is that every claim's evidence
file must exist AND must actually contain the claimed value. String
values match as a substring of the raw text, plus (when the file parses
as JSON) as an exact scalar anywhere in the structure. Numeric values
match any printed spelling (repr, padded and trimmed fixed decimals —
the way Python scripts print numbers) as a standalone number — no
digit, '.' or '-' may touch the match, so a date like 2026-11-10
satisfies no claim of 10, 11 or 2026 — plus a recursive scan of all
numeric scalars in JSON files comparing with math.isclose(rel_tol=1e-3,
abs_tol=1e-9) — Nous v2's check_evidence_item tolerances, so a claim
that passes here passes Nous's own check; an int in the file satisfies
a float claim and vice versa. Files over 50 MB are scanned line by
line, never loaded whole.

Usage: python3 scripts/validate_answer.py <answer.json> [--root DIR]
       [--check-citations [--repo NAME=PATH ...]]
--root is the base for resolving RELATIVE evidence and deliverable paths
(absolute paths are used as-is); it defaults to the parent of the answer
file's directory, so investigations/<slug>/final/answer.json resolves
against investigations/<slug>/ — the study dir.

--check-citations additionally verifies code-citation evidence: for every
claim whose evidence path is under citations/ (relative to the root), the
file's first line must be a header "repo@sha:path:Lstart-Lend" and the rest
must be lines start..end of `git -C <repo> show <sha>:<path>`, byte for byte
(only a trailing-newline difference is tolerated). The repo NAME resolves,
in order: (1) --repo NAME=PATH flags (repeatable); (2) the git toplevel
containing the answer file, when its origin remote basename (minus .git) —
or its folder name when there is no remote — equals NAME; (3) the repos:
map in the machine config (_config.load_machine_config; skipped with a note
when PyYAML is missing or the config is bad). An unresolved name is an
error, never a silent pass. Without the flag, behavior is unchanged.

Exit codes: 0 valid (one OK line per claim plus a summary on stdout);
1 contract violations, one per line on stderr ("<id>: ..." for a claim,
"<key>: ..." or "top level: ..." for the file itself); 2 bad usage,
unreadable file, or invalid JSON. Warnings (stderr, never failures): a
repro containing ";" or a newline — the contract says ONE command; a "|"
pipeline is fine.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from pathlib import Path

TOP_KEYS = ("answer", "deliverable", "repro", "claims")
CLAIM_KEYS = ("id", "metric", "value", "condition", "evidence")

LARGE_FILE_BYTES = 50 * 1024 * 1024  # above this: line-by-line scanning only
REL_TOL, ABS_TOL = 1e-3, 1e-9        # Nous v2 check_evidence_item tolerances
MAX_FIXED_PRECISION = 12             # widest "%.Nf" spelling tried


def is_number(v) -> bool:
    """A number we can search for — not a bool (bool is an int subclass)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def fmt_value(v) -> str:
    """One-line rendering of a claim value for messages: 4100.5, "PD-decider"."""
    return json.dumps(v)


def numeric_forms(v) -> list[str]:
    """Every spelling of a number a Python analysis script might print:
    str/repr plus exact fixed-decimal renderings — padded ("4100.00") and,
    for integral values, trimmed ("4100") — so a float claim matches an int
    printed in the text and vice versa. A form qualifies only when
    float(form) == value; rounded spellings ("0.3" for 0.347) are never
    emitted, so they can never false-positive. Ints beyond float range
    (json.loads parses a >308-digit decimal back as int, and float(v)
    raises OverflowError on them) keep exactly their decimal spelling —
    the only one the text could carry."""
    if not is_number(v):
        return []
    try:
        f = float(v)
    except OverflowError:            # e.g. 10**400 parsed from JSON
        return [str(v)]
    s = repr(f)
    forms = [str(v) if isinstance(v, int) else s]
    trimmed = s.rstrip("0").rstrip(".")  # 4100.0 -> "4100" ("." guards the int part)
    if "." in s and "e" not in s and "E" not in s and trimmed:
        forms.append(trimmed)
    for prec in range(1, MAX_FIXED_PRECISION + 1):
        t = f"{f:.{prec}f}"
        if float(t) == f:  # exact only — a rounded form would false-positive
            forms.append(t)
    return list(dict.fromkeys(forms))  # dedupe, preserving order


def scalar_matches(scalar, value) -> bool:
    """Does one JSON scalar satisfy the iron rule for `value`? Strings match
    exactly; numbers compare with the Nous v2 tolerances. Bools are not
    numbers, so JSON true never satisfies a claim of 1. A comparison
    involving an int beyond float range (which OverflowErrors inside
    math.isclose) falls back to exact equality — no huge int can be within
    rel_tol of a finite value, so the tolerance is moot there."""
    if isinstance(value, str):
        return isinstance(scalar, str) and scalar == value
    if not (is_number(value) and isinstance(scalar, (int, float))
            and not isinstance(scalar, bool)):
        return False
    try:
        return math.isclose(value, scalar, rel_tol=REL_TOL, abs_tol=ABS_TOL)
    except OverflowError:            # huge int beyond float range (10**400)
        return value == scalar


def standalone_number_in(text: str, forms) -> bool:
    """True when one of `forms` (numeric spellings, from numeric_forms)
    appears in `text` as a standalone number: no digit, '.' or '-' may
    touch the match, except that a '.' AFTER it is allowed when no digit
    follows — a sentence-ending period ('settled at 4100.') is not a
    decimal fraction. Without the anchor, a fabricated claim hides inside
    the ambient numeric noise of any log — dates ('2026-11-10' satisfies
    claims of 10, 11 and 2026), pids, ports and sha fragments. The hyphen
    belongs in the anchor classes exactly because of dates and ranges.
    Unit suffixes still match ('p99=10ms', 'took 10s'), and a negative
    spelling matches its whole form ('-5' in 'delta: -5') because the
    lookbehind sees the character before the minus. One deliberate
    tightening: a range endpoint ('values 0-100') no longer satisfies a
    claim of 100 — the evidence must contain the measured value, not an
    interval it falls inside."""
    if not forms:
        return False  # unreachable via text_contains; guards direct callers
    alts = "|".join(re.escape(f)
                    for f in sorted(forms, key=len, reverse=True))
    return re.search(rf"(?<![0-9.-])(?:{alts})(?![0-9-]|\.\d)", text) is not None


def text_contains(text: str, value) -> bool:
    """Substring check in raw text: the value itself for strings, every
    printed spelling for numbers — anchored, via standalone_number_in,
    so a number only matches as a standalone number."""
    if isinstance(value, str):
        return value in text
    return standalone_number_in(text, numeric_forms(value))


def json_contains(doc, value) -> bool:
    """Structural check: the value appearing as a scalar anywhere in a
    parsed JSON document (recursive over dicts and lists)."""
    stack = [doc]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
        elif scalar_matches(node, value):
            return True
    return False


def file_contains(path: Path, value):
    """The iron rule for one evidence file: does it contain `value`?
    Returns (found, error). Files over LARGE_FILE_BYTES are scanned line by
    line (spellings only, but anchored the same way) and never loaded
    whole."""
    try:
        size = path.stat().st_size
    except OSError as e:
        return False, str(e)
    forms = [value] if isinstance(value, str) else numeric_forms(value)
    if size > LARGE_FILE_BYTES:
        try:
            with path.open("r", errors="replace") as fh:
                if isinstance(value, str):
                    if any(form in line for line in fh for form in forms):
                        return True, None
                elif any(standalone_number_in(line, forms) for line in fh):
                    return True, None
        except OSError as e:
            return False, str(e)
        return False, None
    try:
        raw = path.read_text(errors="replace")
    except OSError as e:
        return False, str(e)
    if text_contains(raw, value):
        return True, None
    # Structural search, but only when the file parses as JSON.
    if raw.lstrip(" \t\r\n﻿")[:1] in ("{", "["):
        try:
            doc = json.loads(raw)
        except ValueError:
            doc = None
        if doc is not None and json_contains(doc, value):
            return True, None
    return False, None


def resolve_path(path_str: str, root: Path) -> Path:
    """Relative paths resolve against the root; absolute paths as-is."""
    p = Path(path_str)
    return p if p.is_absolute() else root / p


def validate_answer(data, root: Path):
    """Check a parsed answer.json against the contract. Returns
    (errors, warnings, verified): errors and warnings are one-line strings
    for stderr; verified is a list of (id, metric, value, evidence_path)
    tuples for the per-claim OK lines."""
    errors: list[str] = []
    warnings: list[str] = []
    verified = []
    if not isinstance(data, dict):
        return ([f"top level: must be a JSON object, got {type(data).__name__}"],
                warnings, verified)

    # (2) exactly the four top-level keys — missing and extra both fail
    errors += [f'top level: missing key "{k}"' for k in TOP_KEYS if k not in data]
    errors += [f'top level: unexpected key "{k}"' for k in data if k not in TOP_KEYS]

    # (3) answer: non-empty string
    if "answer" in data and not (isinstance(data["answer"], str) and data["answer"].strip()):
        errors.append("answer: must be a non-empty string")

    # (4) deliverable: null, or a path that must exist (resolved like evidence)
    d = data.get("deliverable")
    if "deliverable" in data and d is not None:
        if not isinstance(d, str) or not d.strip():
            errors.append("deliverable: must be null or a path string")
        else:
            dp = resolve_path(d, root)
            if not dp.exists():
                errors.append(f"deliverable: path does not exist: {dp}")

    # (5) repro: non-empty string; warn when it is not ONE command
    if "repro" in data:
        r = data["repro"]
        if not isinstance(r, str) or not r.strip():
            errors.append("repro: must be a non-empty string")
        elif ";" in r or "\n" in r:
            which = 'a ";"' if ";" in r else "a newline"
            warnings.append(f"repro contains {which} — the contract expects ONE command")

    # (6) claims: non-empty list, each claim exactly the five keys
    claims = data.get("claims", [])
    if "claims" in data:
        if not isinstance(claims, list):
            errors.append("claims: must be a list of claim objects")
            claims = []
        elif not claims:
            errors.append("claims: must be a non-empty list")
    seen_ids = set()
    for i, claim in enumerate(claims):
        where = f"claims[{i}]"
        if not isinstance(claim, dict):
            errors.append(f"{where}: must be an object with exactly the keys "
                          + ", ".join(CLAIM_KEYS))
            continue
        cid = claim.get("id")
        label = cid if isinstance(cid, str) and cid.strip() else where
        errors += [f'{label}: missing key "{k}"' for k in CLAIM_KEYS if k not in claim]
        errors += [f'{label}: unexpected key "{k}"' for k in claim if k not in CLAIM_KEYS]
        if "id" in claim:
            if not isinstance(cid, str) or not cid.strip():
                errors.append(f"{where}: id must be a short unique non-empty label")
            elif cid in seen_ids:
                errors.append(f'claims: duplicate id "{cid}"')
            else:
                seen_ids.add(cid)
        for key in ("metric", "condition"):
            if key in claim and not (isinstance(claim[key], str) and claim[key].strip()):
                errors.append(f"{label}: {key} must be a non-empty string")
        value_ok = False
        if "value" in claim:
            value = claim["value"]
            value_ok = is_number(value) or (isinstance(value, str) and value.strip())
            if not value_ok:
                errors.append(f"{label}: value must be a number (not bool) or a "
                              f"non-empty string, got {type(value).__name__}")

        # (7) the iron rule: the evidence file must exist and contain the value
        if "evidence" not in claim:
            continue  # the missing-key error above already failed this claim
        ev = claim["evidence"]
        if not isinstance(ev, str) or not ev.strip():
            errors.append(f"{label}: evidence must be a non-empty path string")
            continue
        ep = resolve_path(ev, root)
        if not ep.exists():
            errors.append(f"{label}: evidence file does not exist: {ep}")
        elif not ep.is_file():
            errors.append(f"{label}: evidence path is not a file: {ep}")
        elif not value_ok:
            pass  # the value error above already failed this claim
        else:
            found, err = file_contains(ep, claim["value"])
            if err:
                errors.append(f"{label}: evidence file could not be read: {err}")
            elif not found:
                errors.append(f"{label}: evidence file does not contain value "
                              f"{fmt_value(claim['value'])}")
            else:
                verified.append((label, claim.get("metric"), claim["value"], ep))
    return errors, warnings, verified


# --- --check-citations -------------------------------------------------------

CITATION_DIR = "citations"
HEADER_RE = re.compile(
    r"^(?P<repo>[^@\s]+)@(?P<sha>[0-9a-fA-F]{4,64}):(?P<path>\S.*?)"
    r":L(?P<start>\d+)-L(?P<end>\d+)$")
HEADER_FORMAT = "repo@sha:path:Lstart-Lend"


def is_citation_path(ev: str, root: Path) -> bool:
    """Is this evidence path under citations/ (relative to the root)?"""
    p = Path(ev)
    if p.is_absolute():
        try:
            p = p.resolve().relative_to(root)
        except ValueError:
            return False
    parts = [x for x in p.parts if x not in ("", ".")]
    return bool(parts) and parts[0] == CITATION_DIR


def parse_header(line: str):
    """Parse 'repo@sha:path:Lstart-Lend'. Returns (repo, sha, path, start,
    end) or None when malformed (including start < 1 or end < start)."""
    m = HEADER_RE.match(line.strip())
    if not m:
        return None
    start, end = int(m["start"]), int(m["end"])
    if start < 1 or end < start:
        return None
    return m["repo"], m["sha"], m["path"], start, end


def parse_repo_flags(values) -> dict:
    """--repo NAME=PATH values -> {NAME: Path}. Raises ValueError if malformed."""
    repos = {}
    for v in values or []:
        name, sep, path = v.partition("=")
        if not sep or not name.strip() or not path.strip():
            raise ValueError(f'bad --repo value "{v}": expected NAME=PATH')
        repos[name.strip()] = Path(path.strip()).expanduser()
    return repos


def _git_bytes(repo: Path, *args: str):
    """Run git in repo; returns (returncode, stdout bytes, stderr text)."""
    try:
        r = subprocess.run(["git", "-C", str(repo), *args],
                           capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, b"", str(e)
    return r.returncode, r.stdout, r.stderr.decode(errors="replace").strip()


def _import_config():
    """Import the shared _config module (it sits beside this script). Lazy,
    so the validator stays stdlib-only at import time."""
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import _config
    return _config


class RepoResolver:
    """Resolve a citation header's repo NAME to a checkout path (F4 order:
    --repo flags, the answer file's git toplevel, the machine config repos:
    map). Each layer is computed at most once."""

    def __init__(self, flags: dict, answer_path: Path):
        self.flags = flags
        self.answer_dir = answer_path.resolve().parent
        self._top = None      # (toplevel, name) or (None, None) once computed
        self._cfg = None      # (repos map, note) once computed

    def _toplevel(self):
        if self._top is None:
            try:
                _config = _import_config()
                top = _config.repo_toplevel(self.answer_dir)
                self._top = (top, _config.repo_name(top)) if top else (None, None)
            except ImportError:
                self._top = (None, None)
        return self._top

    def _config_repos(self):
        if self._cfg is None:
            try:
                _config = _import_config()
            except ImportError as e:
                self._cfg = ({}, f"machine config skipped: {e}")
                return self._cfg
            where = _config.machine_config_path()
            try:
                cfg = _config.load_machine_config()
            except _config.ConfigError as e:
                self._cfg = ({}, f"machine config skipped: {e}")
                return self._cfg
            repos = cfg.get("repos") or {}
            if not isinstance(repos, dict):
                self._cfg = ({}, f"machine config skipped: {where}: "
                                 "repos must be a mapping of name: path")
            else:
                self._cfg = (repos, None)
        return self._cfg

    def resolve(self, name: str):
        """Returns (path, None) or (None, error-detail string)."""
        if name in self.flags:
            return self.flags[name], None
        top, top_name = self._toplevel()
        if top is not None and top_name == name:
            return top, None
        repos, note = self._config_repos()
        if name in repos and repos[name]:
            return Path(str(repos[name])).expanduser(), None
        tried = ["--repo flags"]
        tried.append(f'the answer file\'s git toplevel ({top}, name "{top_name}")'
                     if top is not None else "the answer file's git toplevel (none)")
        tried.append("the machine config repos: map")
        detail = "tried " + ", ".join(tried)
        if note:
            detail += f"; {note}"
        return None, detail


def check_citation(label: str, ep: Path, resolver: RepoResolver):
    """Verify one citation file. Returns an error string or None."""
    try:
        raw = ep.read_bytes()
    except OSError as e:
        return f"{label}: citation file could not be read: {e}"
    first, nl, body = raw.partition(b"\n")
    header = parse_header(first.decode("utf-8", errors="replace").rstrip("\r"))
    if header is None:
        shown = first.decode("utf-8", errors="replace")[:120]
        return (f'{label}: citation header missing or malformed in {ep}: expected '
                f'"{HEADER_FORMAT}" on the first line, got {json.dumps(shown)}')
    name, sha, path, start, end = header
    where = f"{name}@{sha}:{path}:L{start}-L{end}"
    repo, detail = resolver.resolve(name)
    if repo is None:
        return f'{label}: citation repo "{name}" is unknown ({detail})'
    if not repo.is_dir():
        return f'{label}: citation repo "{name}" path does not exist: {repo}'
    rc, _, err = _git_bytes(repo, "rev-parse", "--git-dir")
    if rc != 0:
        return f'{label}: citation repo "{name}" is not a git repository: {repo}'
    rc, _, _ = _git_bytes(repo, "cat-file", "-e", f"{sha}^{{commit}}")
    if rc != 0:
        return (f'{label}: citation sha {sha} not found in repo "{name}" ({repo}) '
                f"— stale or fabricated citation")
    rc, src, _ = _git_bytes(repo, "show", f"{sha}:{path}")
    if rc != 0:
        return (f'{label}: citation path "{path}" not found at {name}@{sha} '
                f"— stale or fabricated citation")
    src_lines = src.split(b"\n")
    if src_lines and src_lines[-1] == b"":
        src_lines.pop()
    if end > len(src_lines):
        return (f"{label}: citation lines L{start}-L{end} out of range: {path} has "
                f"{len(src_lines)} lines at {name}@{sha}")
    want = src_lines[start - 1:end]
    got = body.split(b"\n")
    if got and got[-1] == b"":
        got.pop()  # trailing newline tolerance — the only one
    for i in range(max(len(want), len(got))):
        a = want[i] if i < len(want) else None
        b = got[i] if i < len(got) else None
        if a != b:
            extra = ""
            if len(got) != len(want):
                extra = f"; quoted {len(got)} lines, header spans {len(want)}"
            return (f"{label}: citation mismatch at line {start + i} of {path} "
                    f"({where}): quoted text differs from the source{extra}")
    return None


def check_citations(data, root: Path, resolver: RepoResolver):
    """Run check_citation over every claim whose evidence is under citations/
    and exists as a file (a missing file is already a contract error).
    Returns (errors, checked) where checked is a list of (label, ep)."""
    errors, checked = [], []
    claims = data.get("claims") if isinstance(data, dict) else None
    if not isinstance(claims, list):
        return errors, checked
    for i, claim in enumerate(claims):
        if not isinstance(claim, dict):
            continue
        cid = claim.get("id")
        label = cid if isinstance(cid, str) and cid.strip() else f"claims[{i}]"
        ev = claim.get("evidence")
        if not isinstance(ev, str) or not ev.strip() or not is_citation_path(ev, root):
            continue
        ep = resolve_path(ev, root)
        if not ep.is_file():
            continue
        err = check_citation(label, ep, resolver)
        if err:
            errors.append(err)
        else:
            checked.append((label, ep))
    return errors, checked


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate an answer.json against the groundwork answer contract.")
    parser.add_argument("answer", help="path to the answer.json to validate")
    parser.add_argument("--root", metavar="DIR",
                        help="base dir for relative evidence/deliverable paths "
                             "(default: the answer dir's parent — the study dir)")
    parser.add_argument("--check-citations", action="store_true",
                        help="also verify citations/ evidence against git: the "
                             "header repo@sha:path:Lstart-Lend and the quoted lines")
    parser.add_argument("--repo", action="append", metavar="NAME=PATH", default=[],
                        help="checkout path for a citation repo name (repeatable; "
                             "used with --check-citations)")
    args = parser.parse_args(argv)
    try:
        repo_flags = parse_repo_flags(args.repo)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2

    answer_path = Path(args.answer)
    if not answer_path.is_file():
        print(f"answer file not found: {answer_path}", file=sys.stderr)
        return 2
    try:
        data = json.loads(answer_path.read_text())
    except OSError as e:
        print(f"cannot read {answer_path}: {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"{answer_path} is not valid JSON: {e}", file=sys.stderr)
        return 2

    # Default root: the parent of the answer file's directory, so
    # investigations/<slug>/final/answer.json resolves against
    # investigations/<slug>/.
    root = Path(args.root) if args.root else answer_path.resolve().parent.parent
    root = root.resolve()

    errors, warnings, verified = validate_answer(data, root)
    cited = []
    if args.check_citations:
        cite_errors, cited = check_citations(
            data, root, RepoResolver(repo_flags, answer_path))
        errors += cite_errors
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    for e in errors:
        print(e, file=sys.stderr)
    if errors:
        return 1
    for cid, metric, value, ep in verified:
        print(f"{cid} OK {metric}={fmt_value(value)} evidence={ep}")
    print(f"OK: {len(verified)} claims verified")
    if args.check_citations:
        for cid, ep in cited:
            print(f"{cid} citation OK evidence={ep}")
        print(f"OK: {len(cited)} citations checked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
