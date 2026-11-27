"""Skill-file validation for document saves."""

from pathlib import Path

from core.skill_validation import is_skill_file
from doceditor.document_save_errors import DocumentSaveError


def invalid_skill_save_error(errors: tuple[str, ...], line: str) -> DocumentSaveError:
    return DocumentSaveError(
        "invalid_skill", line,
        {"message": "Invalid SKILL.md", "errors": list(errors), "line": line},
    )


def refuse_invalid_skill(path: Path, text: str) -> None:
    """Refuse a skill that would not load before writing any bytes."""
    if not is_skill_file(path):
        return
    from infra.skill_save_validation import invalid_skill_write

    if (refusal := invalid_skill_write({path: text.encode()})) is not None:
        raise invalid_skill_save_error(refusal.errors, refusal.line)
