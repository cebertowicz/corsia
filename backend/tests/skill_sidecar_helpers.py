"""Rewrite a legacy ``corsia:`` skill into the ``corsia.yaml`` sidecar form."""

from __future__ import annotations

from typing import Any

import yaml

from core.skill_validation import frontmatter_span

POINTER = "metadata:\n  corsia-data: corsia.yaml\n  corsia-format: '1'\n"


def to_sidecar_form(skill_md: str, corsia: dict[str, Any]) -> tuple[str, str]:
    """Return ``(SKILL.md, corsia.yaml)`` with the block unwrapped into the sidecar."""
    span = frontmatter_span(skill_md)
    assert span is not None, "skill has no frontmatter"
    start, end = span
    kept: list[str] = []
    in_block = False
    for line in skill_md[start:end].splitlines(keepends=True):
        if line.startswith("corsia:"):
            in_block = True
            kept.append(POINTER)
        elif in_block and (line[:1] in {" ", "\t", "\n"} or not line.strip()):
            continue
        else:
            in_block = False
            kept.append(line)
    return skill_md[:start] + "".join(kept) + skill_md[end:], yaml.safe_dump(corsia, sort_keys=False)
