"""Validation and actionable refusals for CORSIA-owned SKILL.md and corsia.yaml saves."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from infra.guidance_git import SaveResult

#: The guidance-save outcome and reason for a write refused before any effect.
REFUSED, INVALID_SKILL_REASON = "refused", "invalid-skill"

_INVALID_SKILL_EXISTING = "Error: {name} not saved — it would not load as a skill. The file is unchanged."
_INVALID_SKILL_NEW = "Error: {name} not saved — it would not load as a skill. Nothing was written."


@dataclass(frozen=True)
class SkillSaveRefusal:
    """An invalid skill write that must not reach the guidance history primitive."""

    errors: tuple[str, ...]
    line: str


def refused_save(
    writes: dict[Path, bytes], *, author: str | None = None, lesson_gate: bool = True,
    workspace: Path | None = None, scope: str | None = None,
) -> SaveResult | None:
    """The guidance-save result refusing an invalid skill write, or a lesson an agent wrote
    outside placement, or None when every write may proceed.

    The lesson rule is on unless a named call site turns it off; a person's own save passes.
    Call outside an open transaction: a caller that rolls back after refusal also loses its outcome fact.
    """
    from infra.guidance_git import SaveResult
    from infra.guidance_messages import is_person_author

    if (refusal := invalid_skill_write(writes)) is not None:
        return SaveResult(REFUSED, INVALID_SKILL_REASON, line=refusal.line, errors=refusal.errors)
    if lesson_gate and not is_person_author(author):
        from infra.lesson_save_gate import lesson_refusal

        return lesson_refusal(writes, author, workspace=workspace, scope=scope)
    return None


def invalid_skill_write(writes: dict[Path, bytes]) -> SkillSaveRefusal | None:
    """Return the first invalid skill write, leaving non-skill data untouched.

    A write to ``SKILL.md`` or its ``corsia.yaml`` validates the whole skill folder:
    the written file together with its partner, from this save or else from disk.
    """
    from core.skill_sidecar import SIDECAR_FILE
    from core.skill_validation import is_skill_file

    checked: set[Path] = set()
    for path in writes:
        sidecar_write = path.name == SIDECAR_FILE and is_skill_file(path.with_name("SKILL.md"))
        if not (is_skill_file(path) or sidecar_write) or path.parent in checked:
            continue
        checked.add(path.parent)
        errors = _folder_errors(path.parent, writes)
        if errors:
            return SkillSaveRefusal(errors=errors, line=invalid_skill_line(path, errors))
    return None


def _folder_errors(folder: Path, writes: dict[Path, bytes]) -> tuple[str, ...]:
    """Validate one skill folder as it would stand after these writes."""
    from core.skill_sidecar import SIDECAR_FILE, read_sidecar
    from core.skill_validation import validate_skill_bundle

    skill_path, sidecar_path = folder / "SKILL.md", folder / SIDECAR_FILE
    if skill_path in writes:
        text, errors = _decoded(writes[skill_path], "SKILL.md")
    elif skill_path.is_file():
        text, errors = _decoded(skill_path.read_bytes(), "SKILL.md")
    else:
        return ()  # A sidecar with no SKILL.md yet: the SKILL.md save validates both.
    problem = None
    if sidecar_path in writes:
        sidecar, sidecar_errors = _decoded(writes[sidecar_path], SIDECAR_FILE)
        errors += sidecar_errors
    else:
        sidecar, problem = read_sidecar(folder)
    if errors:
        return errors
    files = None if sidecar is None else {SIDECAR_FILE: sidecar}
    return tuple(validate_skill_bundle(text, files, sidecar_problem=problem).errors)


def _decoded(data: bytes, name: str) -> tuple[str, tuple[str, ...]]:
    try:
        return data.decode("utf-8"), ()
    except UnicodeDecodeError as exc:
        return "", (f"{name} must be UTF-8 text: {exc}",)


def invalid_skill_line(path: Path, errors: tuple[str, ...]) -> str:
    """Render an actionable refusal without guessing an unknown repair."""
    lead = (_INVALID_SKILL_EXISTING if path.exists() else _INVALID_SKILL_NEW).format(name=path.name)
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
