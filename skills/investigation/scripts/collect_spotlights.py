#!/usr/bin/env python3
"""Collect a finished Spotlights run into the study: copies + code citations.

Reads a Spotlights output folder (--run-dir, typically
<runs dir>/<repo>/<slug>/spotlights-<id>/) READ-ONLY and:

  1. copies into <study>/spotlights/ the parts the study keeps:
       result.json            the SpotlightReport run output. Deep-research
                              runs write {report: SpotlightReport, module_runs,
                              per_module_telemetry, manager_issues,
                              renderer_result, extractor_invocation}; the
                              signal pipeline writes a bare SpotlightReport as
                              spotlight_report.json (copied as result.json).
       run_manifest.json      the public run manifest (target repo_url +
                              commit_sha, objective, spotlights engine
                              commit/config, models_used, timing, cost)
       sorted_candidates.md   from /spotlights-sort-candidates (at the run
       sorted_candidates.json dir or its sorted/ subdir); the .json, when
                              present, gives the ranked order
  2. for the top-N candidates (ranked order from sorted_candidates.json, else
     the order of ids in sorted_candidates.md, else result.json order) drafts
     one code-citation claim each: citations/<claim-id>.txt holds the header
     `repo@sha:path:Lstart-Lend` and then lines start..end exactly as
     `git -C <repo> show <sha>:<path>` prints them, so the quote is verbatim
     by construction. The citation is the candidate's first location's first
     span. The claim value is the first non-blank quoted line, stripped (a
     verbatim substring of the citation file); condition is
     "<repo> @ <sha[:8]>, <path>:L<start>"; evidence citations/<claim-id>.txt;
  3. merges a section under collection.json["spotlights"] (see _collection.py).

A Spotlights ESTIMATE (estimated_impact, its explanation, sort scores and
rationales, proposal expected effects, cost) never becomes a claim value.
Proposals are hypotheses: they are listed in the section labelled as such,
never drafted as claims.

The sha is the manifest's target.commit_sha unless --sha overrides it; the
repo is --repo, else the study's git toplevel. A claim is verified:true when
its citation was written from that repo at that sha; candidates whose span
can't be cited (missing sha, path not at sha, lines out of range) give a note
and no claim.

Usage: python3 scripts/collect_spotlights.py --study DIR --run-dir DIR
                                             [--top N] [--repo PATH] [--sha SHA]

Exit 0 = collected. Exit 1 = could not run: no study, no run dir, no readable
result.json / spotlight_report.json, no repo, or a bad collection.json.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True  # an install inside a target repo must not dirty it (D4)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _collection  # noqa: E402
import _config  # noqa: E402
from _telemetry import sha256_file  # noqa: E402

DEFAULT_TOP = 5
CAND_ID_RE = re.compile(r"cand-[A-Za-z0-9._-]+?-\d{4}")


class CouldNotRun(Exception):
    pass


# ── reading the run dir ──────────────────────────────────────────────────


def _load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def find_result(run_dir: Path) -> Path:
    for name in ("result.json", "spotlight_report.json", "report/spotlight_report.json"):
        p = run_dir / name
        if p.is_file():
            return p
    raise CouldNotRun(f"no result.json or spotlight_report.json in {run_dir}")


def report_of(result) -> dict:
    """The SpotlightReport inside result.json (deep-research wrapper or bare)."""
    if isinstance(result, dict) and isinstance(result.get("report"), dict):
        return result["report"]
    if isinstance(result, dict) and "candidates" in result and "run" in result:
        return result
    raise CouldNotRun("result.json holds no SpotlightReport (no report.candidates)")


def find_sorted(run_dir: Path, name: str):
    for p in (run_dir / name, run_dir / "sorted" / name):
        if p.is_file():
            return p
    return None


def ranked_ids(candidates: list, sorted_json, sorted_md_text) -> tuple[list, str]:
    known = [c.get("id") for c in candidates if isinstance(c, dict)]
    known_set = set(known)
    order, source = [], "result.json order (no sorted_candidates found)"
    if isinstance(sorted_json, dict) and isinstance(sorted_json.get("candidates"), list):
        order = [c.get("id") for c in sorted_json["candidates"] if isinstance(c, dict)]
        source = "sorted_candidates.json"
    elif sorted_md_text:
        order = CAND_ID_RE.findall(sorted_md_text)
        source = "sorted_candidates.md (order of first appearance)"
    seen, out = set(), []
    for cid in order + known:
        if cid in known_set and cid not in seen:
            seen.add(cid)
            out.append(cid)
    return out, source


MAX_FIXED_PRECISION = 12          # widest "%.Nf" spelling tried (as validate_answer)


def numeric_spellings(v) -> set:
    """Every spelling of a numeric estimate a quoted source line could carry:
    str/json.dumps/repr plus the trimmed integral form ("5" for 5.0), exact
    fixed-decimal renderings ("0.50".."0.500000000000", and their
    leading-zero-trimmed ".50" variants), and the exponent spellings
    ("5e-1", "5E-1", "5e-01", "0.5e0", "5.0e-1", ...). Mirrors
    validate_answer.numeric_forms' shape (kept local rather than imported so
    this collector never loads the answer validator) and extends it to
    exponent forms, because pick_value returns whole stripped lines: a bare
    numeric literal early in a quoted span becomes the claim value whatever
    spelling it uses. A form qualifies only when float(form) == value, so a
    rounded or approximate form is never emitted and can never over-block.
    Non-finite estimates (NaN/Infinity, which json.dump writes and json.loads
    reads back) keep only their base spellings ("nan"/"NaN"/"Infinity"/"inf"),
    mirroring numeric_forms' tolerance of them: "{f:e}" has no exponent part
    to read, and the round-trip filter cannot judge NaN (NaN != NaN), so any
    exponent-extension of them would be guesswork. Ints beyond float range
    (json.loads parses a >308-digit decimal back as int) likewise crash
    float(v) with OverflowError, so they too fall back to their base
    spellings."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return set()
    try:
        f = float(v)
    except OverflowError:            # e.g. 10**400 parsed from JSON
        return {json.dumps(v), str(v)}
    out = {json.dumps(v), str(v), repr(f)}
    if not math.isfinite(f):
        return out
    s = repr(f)
    trimmed = s.rstrip("0").rstrip(".")  # 5.0 -> "5" ("." guards the int part)
    if "." in s and "e" not in s and "E" not in s and trimmed:
        out.add(trimmed)
        out.add(trimmed.lstrip("0") if trimmed.startswith("0") else trimmed)  # 0.5 -> ".5"
    for prec in range(1, MAX_FIXED_PRECISION + 1):
        t = f"{f:.{prec}f}"  # exact only — a rounded form would over-block
        if float(t) == f:
            out.add(t)
            if t.startswith("0."):
                out.add(t[1:])  # "0.50" -> ".50"
    # exponent spellings: the canonical scientific form read off "{f:e}" and
    # its one-digit point shifts, signed and unsigned exponents, 2- and
    # 3-digit zero padding, e/E, with and without the fractional mantissa
    # ("5e-1", "5.0e-1", "0.5e0", "1e+01")
    sci = f"{f:e}"            # e.g. "5.000000e-01"
    mant, _, exp = sci.partition("e")
    exp_i = int(exp)
    # one-digit decimal-point shifts of the mantissa, so "0.5e0" (mantissa
    # 0.5, exponent 0) is covered for the canonical "5e-1": the significant
    # digits move across the point and the exponent compensates the other
    # way (0.5e0 == 5e-1), so both exponent values are emitted, each bare,
    # explicitly signed, and zero-padded to 2 and 3 digits.
    exps = set()
    for e in (exp_i, exp_i + 1):
        exps.update({str(e), f"{e:+d}",
                     f"{'-' if e < 0 else '+'}{abs(e):02d}",
                     f"{'-' if e < 0 else '+'}{abs(e):03d}"})
    sig = mant.rstrip("0").rstrip(".") or "0"    # "5.000000" -> "5"
    digits = sig.replace(".", "")
    mants = {mant, sig, digits}
    mants.add(f"0.{digits}")                     # "5" -> "0.5" (exponent +1)
    if len(digits) > 1:
        mants.add(f"{digits[0]}.{digits[1:]}")   # "2861085" -> "2.861085"
    mants.add(f"{digits}.0")                     # "5" -> "5.0", "1" -> "1.0"
    for e in exps:
        for m in mants:
            for sep in ("e", "E"):
                out.add(f"{m}{sep}{e}")
    return {t for t in out if _parses_exactly(t, f)}


def _parses_exactly(text: str, f: float) -> bool:
    """float(text) == f, tolerating the leading-dot (".5") spelling Python's
    float() already accepts; anything else (nan, inf, overflow) is dropped."""
    try:
        return float(text) == f
    except ValueError:
        return False


def estimate_values(report: dict, sorted_json, manifest) -> set:
    """Every Spotlights estimate in the run; none may be a claim value."""
    vals = set()

    def add(v):
        if v is None or isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            vals.update(numeric_spellings(v))
        elif isinstance(v, str) and v.strip():
            vals.add(v.strip())

    for c in report.get("candidates") or []:
        if not isinstance(c, dict):
            continue
        add(c.get("estimated_impact"))
        add(c.get("estimated_impact_explanation"))
        for p in c.get("proposals") or []:
            if isinstance(p, dict):
                add(p.get("expected_effect"))
    add((report.get("run") or {}).get("cost_usd"))
    if isinstance(sorted_json, dict):
        for c in sorted_json.get("candidates") or []:
            if isinstance(c, dict):
                for k in ("score", "impact", "rationale", "rank"):
                    add(c.get(k))
    if isinstance(manifest, dict):
        add((manifest.get("cost") or {}).get("amount_usd"))
    return vals


# ── git / citations ──────────────────────────────────────────────────────


def git_bytes(repo: Path, *args: str):
    try:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def source_lines(repo: Path, sha: str, path: str):
    """File lines at sha, split exactly as validate_answer --check-citations does."""
    raw = git_bytes(repo, "show", f"{sha}:{path}")
    if raw is None:
        return None
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    return lines


def first_span(cand: dict):
    for loc in cand.get("locations") or []:
        if not isinstance(loc, dict) or not loc.get("file"):
            continue
        for sp in loc.get("spans") or []:
            if isinstance(sp, dict) and "line_start" in sp and "line_end" in sp:
                return loc["file"], sp
    return None, None


def pick_value(lines: list, forbidden: set):
    for b in lines:
        s = b.decode("utf-8", errors="replace").strip()
        if s and s not in forbidden:
            return s
    return None


def claim_id_for(cand_id: str) -> str:
    return f"cite-{cand_id}"


# ── collect ──────────────────────────────────────────────────────────────


def collect(study: Path, run_dir: Path, top: int, repo: Path, sha_override) -> dict:
    notes: list[str] = []
    result_src = find_result(run_dir)
    result = _load_json(result_src)
    if result is None:
        raise CouldNotRun(f"{result_src} is not valid JSON")
    report = report_of(result)
    candidates = [c for c in report.get("candidates") or [] if isinstance(c, dict)]

    manifest_src = run_dir / "run_manifest.json"
    manifest = _load_json(manifest_src) if manifest_src.is_file() else None
    if manifest is None:
        notes.append(f"no readable run_manifest.json in {run_dir}")
    sorted_md = find_sorted(run_dir, "sorted_candidates.md")
    sorted_js = find_sorted(run_dir, "sorted_candidates.json")
    if sorted_md is None:
        notes.append("no sorted_candidates.md: run /spotlights-sort-candidates on result.json")
    sorted_json = _load_json(sorted_js) if sorted_js else None

    # 1. copies
    out = study / "spotlights"
    out.mkdir(exist_ok=True)
    copies = {}
    for src, name in ((result_src, "result.json"),
                      (manifest_src if manifest is not None else None, "run_manifest.json"),
                      (sorted_md, "sorted_candidates.md"),
                      (sorted_js, "sorted_candidates.json")):
        if src is None:
            continue
        dst = out / name
        shutil.copyfile(src, dst)
        copies[f"spotlights/{name}"] = {"from": str(src), "sha256": sha256_file(dst)}
    if sorted_md is not None:
        notes.append("sorted_candidates.md links to candidate pages relative to the run "
                     "dir; those links don't resolve from the study copy")

    # 2. citations
    target = (manifest or {}).get("target") or {}
    sha = sha_override or target.get("commit_sha") or None
    repo_label = _config.repo_name(repo)
    claims, cited = [], []
    order, order_source = ranked_ids(candidates, sorted_json,
                                     sorted_md.read_text(errors="replace") if sorted_md else None)
    by_id = {c.get("id"): c for c in candidates}
    forbidden = estimate_values(report, sorted_json, manifest)
    full_sha = None
    if not sha:
        notes.append("no target commit sha (manifest target.commit_sha empty, no --sha); "
                     "no citations drafted")
    else:
        resolved = git_bytes(repo, "rev-parse", "--verify", f"{sha}^{{commit}}")
        if resolved is None:
            notes.append(f"sha {sha} is not a commit in {repo}; no citations drafted "
                         "(fetch it, or pass --repo for the target checkout)")
        else:
            full_sha = resolved.decode().strip()
    source_root = ((report.get("project_tree") or {}).get("repository") or {}).get("source_root")
    if full_sha:
        cdir = study / "citations"
        for rank, cid in enumerate(order[:max(top, 0)], start=1):
            cand = by_id[cid]
            path, span = first_span(cand)
            if path is None:
                notes.append(f"{cid}: no file span to cite")
                continue
            lines = source_lines(repo, full_sha, path)
            if lines is None and source_root:
                alt = f"{source_root.rstrip('/')}/{path}"
                lines = source_lines(repo, full_sha, alt)
                path = alt if lines is not None else path
            if lines is None:
                notes.append(f"{cid}: {path} not found at {repo_label}@{full_sha[:8]}")
                continue
            start, end = int(span["line_start"]), int(span["line_end"])
            if start < 1 or end < start or end > len(lines):
                notes.append(f"{cid}: lines L{start}-L{end} out of range ({path} has "
                             f"{len(lines)} lines at {full_sha[:8]})")
                continue
            quoted = lines[start - 1:end]
            value = pick_value(quoted, forbidden)
            if value is None:
                notes.append(f"{cid}: L{start}-L{end} of {path} has no usable line to quote")
                continue
            claim_id = claim_id_for(cid)
            cdir.mkdir(exist_ok=True)
            header = f"{repo_label}@{full_sha}:{path}:L{start}-L{end}\n".encode()
            (cdir / f"{claim_id}.txt").write_bytes(header + b"\n".join(quoted) + b"\n")
            claims.append({
                "id": claim_id,
                "metric": f"code site: {span.get('symbol') or path}",
                "value": value,
                "condition": f"{repo_label} @ {full_sha[:8]}, {path}:L{start}",
                "evidence": f"citations/{claim_id}.txt",
                "verified": True,
                "candidate_id": cid,
            })
            cited.append(cid)

    # Candidates and their proposals, for the answer's prose — hypotheses, not claims.
    rank_of = {cid: i for i, cid in enumerate(order, start=1)}
    cand_summary = []
    for cid in order[:max(top, 0)]:
        c = by_id[cid]
        path, span = first_span(c)
        cand_summary.append({
            "id": cid, "rank": rank_of[cid], "module": c.get("module_qualified_name"),
            "file": path, "span": span, "cited": cid in cited,
            "spotlights_estimate_not_a_claim": {
                "estimated_impact": c.get("estimated_impact")},
            "proposals_are_hypotheses": [
                {"id": p.get("id"), "title": p.get("title"), "source": p.get("source")}
                for p in c.get("proposals") or [] if isinstance(p, dict)],
        })

    run = report.get("run") or {}
    return {
        "run_dir": str(run_dir),
        "run_id": run.get("run_id") or (manifest or {}).get("run_id"),
        "pipeline": run.get("pipeline"),
        "objective": (report.get("context") or {}).get("objective"),
        "target": {"repo_url": target.get("repo_url"), "commit_sha": target.get("commit_sha"),
                   "cited_sha": full_sha, "repo": str(repo), "repo_name": repo_label},
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "copies": copies,
        "n_candidates": len(candidates),
        "top": top,
        "rank_source": order_source,
        "candidates_top": cand_summary,
        "notes": notes,
        "claims_draft": claims,
    }


def collect_run(study, run_dir, *, top: int = DEFAULT_TOP, repo=None,
                sha=None) -> tuple[dict, Path]:
    """Collect one run dir into the study and merge the section (the CLI's work).

    Reusable entry point (the runtime's spotlights-static adapter calls it).
    `repo` defaults to the study's git toplevel; `sha` to the manifest's
    target.commit_sha. Returns (section, collection.json path). Raises
    CouldNotRun or _collection.CollectionError; never touches the run dir.
    """
    study = Path(study).expanduser()
    run_dir = Path(run_dir).expanduser()
    if not study.is_dir():
        raise CouldNotRun(f"--study {study} is not a directory")
    if not run_dir.is_dir():
        raise CouldNotRun(f"--run-dir {run_dir} is not a directory")
    study, run_dir = study.resolve(), run_dir.resolve()
    if repo is not None:
        repo = Path(repo).expanduser().resolve()
        top_of = _config.repo_toplevel(repo)
        if top_of is None:
            raise CouldNotRun(f"--repo {repo} is not a git repository")
        repo = top_of
    else:
        repo = _config.repo_toplevel(study)
        if repo is None:
            raise CouldNotRun(f"--study {study} is not inside a git repo; pass --repo")
    section = collect(study, run_dir, top, repo, sha)
    path = _collection.merge_section(study, "spotlights", section)
    return section, path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--study", required=True, metavar="DIR")
    parser.add_argument("--run-dir", required=True, metavar="DIR",
                        help="the Spotlights output folder (read-only)")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP, metavar="N",
                        help=f"cite the top N candidates (default {DEFAULT_TOP})")
    parser.add_argument("--repo", metavar="PATH", default=None,
                        help="target repo checkout to cite from (default: the study's "
                             "git toplevel)")
    parser.add_argument("--sha", default=None,
                        help="commit to cite at (default: run_manifest target.commit_sha)")
    args = parser.parse_args(argv)
    try:
        section, path = collect_run(args.study, args.run_dir, top=args.top,
                                    repo=args.repo, sha=args.sha)
    except (CouldNotRun, _collection.CollectionError) as e:
        print(e, file=sys.stderr)
        return 1
    print(f"{path}: spotlights — {len(section['claims_draft'])} citation claim(s), "
          f"{len(section['notes'])} note(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
