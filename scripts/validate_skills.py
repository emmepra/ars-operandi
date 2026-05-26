#!/usr/bin/env python3
"""Validate Ars Operandi skill folders without external dependencies."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"


def parse_frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        raise ValueError("missing YAML frontmatter")

    data: dict[str, str] = {}
    for raw_line in match.group(1).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"invalid frontmatter line: {raw_line!r}")
        key, value = line.split(":", 1)
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        data[key.strip()] = value
    return data


def validate_skill(path: Path) -> list[str]:
    errors: list[str] = []
    skill_md = path / "SKILL.md"
    if not skill_md.exists():
        return [f"{path.name}: missing SKILL.md"]

    try:
        frontmatter = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"{path.name}: {exc}"]

    name = frontmatter.get("name", "")
    description = frontmatter.get("description", "")
    if name != path.name:
        errors.append(f"{path.name}: frontmatter name {name!r} does not match folder")
    if not re.fullmatch(r"[a-z0-9-]{1,64}", name):
        errors.append(f"{path.name}: invalid skill name {name!r}")
    if not description:
        errors.append(f"{path.name}: missing description")
    if len(description) > 1024:
        errors.append(f"{path.name}: description exceeds 1024 characters")

    openai_yaml = path / "agents" / "openai.yaml"
    if openai_yaml.exists() and "$" not in openai_yaml.read_text(encoding="utf-8"):
        errors.append(f"{path.name}: agents/openai.yaml default_prompt should mention $skill")

    return errors


def main() -> int:
    if not SKILLS.exists():
        print("skills/ not found", file=sys.stderr)
        return 1

    errors: list[str] = []
    for path in sorted(p for p in SKILLS.iterdir() if p.is_dir()):
        errors.extend(validate_skill(path))

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    print("All skills valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
