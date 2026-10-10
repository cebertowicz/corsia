"""Per-skill token estimates for the skills index's inventory row.

Display only. The index is delivered as one block and counted once; this
splits that count across the skills it lists so the context sheet can show
each skill's cost. It reads the text that was delivered, with the Skills
estimator already used for lessons, and never counts more than the block.
"""

from __future__ import annotations

from typing import Any

from core.lesson_index import TRIAL_MARK, descriptor_estimates


def _line_for(name: str, lines: list[str]) -> str | None:
    """The delivered index line of one skill, or None when it is not listed."""
    for prefix in (f"- {name}:", f"- {name}{TRIAL_MARK}:"):
        for line in lines:
            if line.startswith(prefix):
                return line
    return None


def with_skill_tokens(
    metadata: dict[str, Any], content: str, total_tokens: int, model: str | None,
) -> dict[str, Any]:
    """Return ``metadata`` with a ``prompt_tokens`` on each listed skill.

    A skill whose line is not in ``content`` gets none: its cost is unknown,
    not zero. The block's own header and notes stay in the remainder.
    """
    skills = metadata.get("skills")
    if not isinstance(skills, list):
        return metadata
    lines = content.splitlines()
    found = {
        str(index): line
        for index, entry in enumerate(skills)
        if (line := _line_for(str(entry.get("name", "")), lines)) is not None
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
