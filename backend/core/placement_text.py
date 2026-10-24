"""Text transformations for placement destinations.

Placement destination mapping lives in ``placement_decide``; these helpers only
produce the text that its already-selected destination will receive.
"""

from __future__ import annotations

import re


def lesson_count(text: str) -> int:
    """Count legacy ``## Lessons`` dash-items for placement caps only."""
    inside, count = False, 0
    for line in text.split("\n"):
        if line.startswith("## "):
            inside = bool(re.match(r"^## Lessons\s*$", line))
        elif inside and line.lstrip().startswith("- "):
            count += 1
    return count


def append_lesson(text: str | None, line: str, day: str) -> tuple[str, int]:
    """Add one dated lesson under exact ``## Lessons``; return text and legacy count."""
    body = text if text is not None else "# Lessons\n"
    lesson = f"- {day}: {line}"
    if not re.search(r"^## Lessons\s*$", body, re.MULTILINE):
        body = body.rstrip("\n") + "\n\n## Lessons\n"
    lines = body.rstrip("\n").split("\n")
    start = next(i for i, row in enumerate(lines) if re.match(r"^## Lessons\s*$", row))
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, lesson)
    result = "\n".join(lines) + "\n"
    return result, lesson_count(result)


def append_to_skill(text: str | None, name: str, line: str, description: str | None) -> str:
    """Add one line to a skill, creating it with frontmatter when new."""
    if text is None:
        from core.skill_validation import skill_template

        template = skill_template(name, description or line)
        frontmatter, _title, _placeholder = template.partition(f"# {name}\n")
        return frontmatter.rstrip("\n") + f"\n\n# {name}\n\n- {line}\n"
    return text.rstrip("\n") + f"\n- {line}\n"


def append_to_reference(text: str, line: str) -> str:
    """Add one line to the end of an agent reference file."""
    return text.rstrip("\n") + f"\n\n- {line}\n"
