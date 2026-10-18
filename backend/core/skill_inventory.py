"""Per-skill token estimates for the skills index's inventory row.

Display only. The index is delivered as one block and counted once; this
splits that count across the skills it lists so the context sheet can show
each skill's cost. It reads the text that was delivered, with the Skills
estimator already used for lessons, and never counts more than the block.
"""

from __future__ import annotations

from typing import Any

from core.lesson_index import TRIAL_MARK, descriptor_estimates


def _line_for(entry: dict[str, Any], lines: list[str]) -> str | None:
    """The delivered text charged to one listed skill, or None when unknown."""
    name = str(entry.get("name", ""))
    if entry.get("listed") == "name":
        path = entry.get("path")
        return f"{name} ({path})" if isinstance(path, str) and path else None
    for prefix in (f"- {name}:", f"- {name}{TRIAL_MARK}:"):
        for line in lines:
            if line.startswith(prefix):
                return line
    return None


def with_skill_tokens(
    metadata: dict[str, Any], content: str, total_tokens: int, model: str | None,
) -> dict[str, Any]:
    """Return ``metadata`` with a ``prompt_tokens`` on each listed skill.

    A skill whose text is not in ``content`` gets none: its cost is unknown,
    not zero. A names-only skill is charged for its own ``name (path)`` entry;
    the overflow lead-in and the block's notes stay in the remainder.
    """
    skills = metadata.get("skills")
    if not isinstance(skills, list):
        return metadata
    lines = content.splitlines()
    found = {
        str(index): line
        for index, entry in enumerate(skills)
        if isinstance(entry, dict) and (line := _line_for(entry, lines)) is not None
    }
    estimates = descriptor_estimates(
        [{"id": key, "index_line": line} for key, line in found.items()],
        skills_tokens=total_tokens, model=model,
    )
    return {
        **metadata,
        "skills": [
            {**entry, "prompt_tokens": estimates[str(index)]} if str(index) in estimates else entry
            for index, entry in enumerate(skills)
        ],
    }
