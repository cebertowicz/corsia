"""Validation and actionable refusals for CORSIA-owned SKILL.md saves."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.guidance_git import SaveResult

#: The guidance-save outcome and reason for a write refused before any effect.
REFUSED, INVALID_SKILL_REASON = "refused", "invalid-skill"

_INVALID_SKILL_EXISTING = "Error: SKILL.md not saved — it would not load as a skill. The file is unchanged."
_INVALID_SKILL_NEW = "Error: SKILL.md not saved — it would not load as a skill. Nothing was written."


@dataclass(frozen=True)
class SkillSaveRefusal:
    """An invalid skill write that must not reach the guidance history primitive."""

    errors: tuple[str, ...]
    line: str


def refused_save(writes: dict[Path, bytes]) -> SaveResult | None:
    """The guidance-save result refusing an invalid skill write, or None when every write may proceed."""
    from infra.guidance_git import SaveResult

    if (refusal := invalid_skill_write(writes)) is None:
        return None
    return SaveResult(REFUSED, INVALID_SKILL_REASON, line=refusal.line, errors=refusal.errors)


def invalid_skill_write(writes: dict[Path, bytes]) -> SkillSaveRefusal | None:
    """Return the first invalid skill write, leaving non-skill data untouched."""
    from core.skill_validation import is_skill_file, validate_skill_text

    for path, data in writes.items():
        if not is_skill_file(path):
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            errors = (f"SKILL.md must be UTF-8 text: {exc}",)
        else:
            validation = validate_skill_text(text)
            if validation.ok:
                continue
            errors = tuple(validation.errors)
        return SkillSaveRefusal(errors=errors, line=invalid_skill_line(path, errors))
    return None


def invalid_skill_line(path: Path, errors: tuple[str, ...]) -> str:
    """Render an actionable refusal without guessing an unknown repair."""
    lead = _INVALID_SKILL_EXISTING if path.exists() else _INVALID_SKILL_NEW
    lines = [lead, str(path), *(f"- {error}" for error in errors)]
    fix = skill_fix(errors)
    if fix:
        lines.append(f"Fix: {fix}")
    return "\n".join(lines)


def skill_fix(errors: tuple[str, ...]) -> str | None:
    """Return a repair only for validation classes with an unambiguous fix."""
    if any(error.startswith("frontmatter parse failed:") for error in errors):
        return "correct the YAML frontmatter at the reported location. Quote text values containing ': ' or use a block scalar where needed, then save again."
    if "description is required" in errors:
        return "add a non-empty description, then save again."
    if "name is required" in errors:
        return "add a non-empty name that matches the skill directory, then save again."
    if any("must match" in error and "name" in error for error in errors):
        return "make the name match the skill directory, then save again."
    if any("too large" in error or "exceeds" in error for error in errors):
        return "shorten the skill, then save again."
    return None
