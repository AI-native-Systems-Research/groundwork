#!/usr/bin/env python3
"""Check a skill directory against the Agent Skills spec (agentskills.io/specification).

Covers what the official `skills-ref` validator does *not*: SKILL.md line count,
and `metadata` being a string->string map. It also repeats skills-ref's
frontmatter checks, so the check still runs when skills-ref is not installed
(it needs Python 3.11+, and CI's unit matrix includes 3.10).

Stdlib only. Uses PyYAML for frontmatter if it is importable, otherwise a
minimal parser that handles flat keys, `>-`/`|` block scalars and one-level maps.

Usage: check_skill_spec.py skills/investigation   (exit 0 = valid, 1 = errors)
"""
from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_COMPATIBILITY = 500
MAX_LINES = 500  # "Keep your main SKILL.md under 500 lines."
ALLOWED = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")  # no leading/trailing/double hyphens


def split_frontmatter(text: str) -> tuple[str, str] | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end == -1:
        return None
    return text[4:end], text[end + 4:]


def _fallback_yaml(src: str) -> dict:
    data: dict = {}
    lines = src.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not m:
            raise ValueError(f"cannot parse frontmatter line: {line!r}")
        key, rest = m.group(1), m.group(2).strip()
        i += 1
        block = []
        while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
            block.append(lines[i])
            i += 1
        if rest in (">", ">-", "|", "|-"):
            body = [b.strip() for b in block]
            data[key] = (" " if rest.startswith(">") else "\n").join(b for b in body if b)
        elif rest == "" and block:
            sub = {}
            for b in block:
                if not b.strip():
                    continue
                sm = re.match(r"^\s+([^:]+):\s*(.*)$", b)
                if not sm:
                    raise ValueError(f"cannot parse map line: {b!r}")
                sub[sm.group(1).strip()] = sm.group(2).strip().strip("\"'")
            data[key] = sub
        else:
            data[key] = rest.strip("\"'")
    return data


def parse_yaml(src: str) -> dict:
    try:
        import yaml  # type: ignore
    except ImportError:
        return _fallback_yaml(src)
    data = yaml.safe_load(src)
    if not isinstance(data, dict):
        raise ValueError("frontmatter is not a mapping")
    return data


def check(skill_dir: Path) -> list[str]:
    errs: list[str] = []
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return [f"missing {skill_md}"]
    text = skill_md.read_text(encoding="utf-8")
    n_lines = len(text.splitlines())
    if n_lines >= MAX_LINES:
        errs.append(f"SKILL.md has {n_lines} lines; the spec says keep it under {MAX_LINES}")
    parts = split_frontmatter(text)
    if parts is None:
        return errs + ["SKILL.md has no YAML frontmatter (must start with '---' and close with '---')"]
    try:
        fm = parse_yaml(parts[0])
    except Exception as e:  # noqa: BLE001
        return errs + [f"frontmatter does not parse: {e}"]

    extra = set(fm) - ALLOWED
    if extra:
        errs.append(f"unexpected frontmatter fields: {sorted(extra)} (allowed: {sorted(ALLOWED)})")

    name = fm.get("name")
    if not isinstance(name, str) or not name.strip():
        errs.append("'name' is required and must be a non-empty string")
    else:
        name = unicodedata.normalize("NFKC", name.strip())
        if len(name) > MAX_NAME:
            errs.append(f"'name' is {len(name)} chars; max {MAX_NAME}")
        if not NAME_RE.match(name):
            errs.append(f"'name' {name!r} must be lowercase a-z, 0-9 and single hyphens, "
                        "not starting or ending with a hyphen")
        if name != skill_dir.resolve().name:
            errs.append(f"'name' {name!r} must match the folder name {skill_dir.resolve().name!r}")

    desc = fm.get("description")
    if not isinstance(desc, str) or not desc.strip():
        errs.append("'description' is required and must be a non-empty string")
    elif len(desc) > MAX_DESCRIPTION:
        errs.append(f"'description' is {len(desc)} chars; max {MAX_DESCRIPTION}")

    if "license" in fm and (not isinstance(fm["license"], str) or not fm["license"].strip()):
        errs.append("'license' must be a non-empty string")

    if "compatibility" in fm:
        c = fm["compatibility"]
        if not isinstance(c, str) or not c.strip():
            errs.append("'compatibility' must be a non-empty string")
        elif len(c) > MAX_COMPATIBILITY:
            errs.append(f"'compatibility' is {len(c)} chars; max {MAX_COMPATIBILITY}")

    if "metadata" in fm:
        md = fm["metadata"]
        if not isinstance(md, dict):
            errs.append("'metadata' must be a map")
        else:
            for k, v in md.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    errs.append(f"'metadata' must map strings to strings; {k!r}: {v!r} "
                                "(quote numbers, e.g. version: \"2.0.0\")")

    if "allowed-tools" in fm and not isinstance(fm["allowed-tools"], str):
        errs.append("'allowed-tools' must be a space-separated string")

    return errs


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    skill_dir = Path(argv[1])
    errs = check(skill_dir)
    if errs:
        for e in errs:
            print(f"FAIL {skill_dir}: {e}")
        return 1
    print(f"OK {skill_dir}: frontmatter valid, SKILL.md under {MAX_LINES} lines")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
