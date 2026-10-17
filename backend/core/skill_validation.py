"""Validation and parsing for ``SKILL.md`` documents.

Skill validation has one small home here; ``core.skill_resolution`` owns scope
resolution.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.lesson_trials import LESSON_KEY, lesson_errors
from core.skill_sidecar import (
    BLOCK_ONLY_KEYS,
    SIDECAR_FILE,
    parse_sidecar,
    pointer_errors,
    read_sidecar,
)
from core.skill_workflow_validation import text_value, validate_workflow

ALLOWED_HANDLER_KINDS = {
    "instruction",
    "cli",
    "native_tool",
    "connection_action",
    "script",
}
_HANDLER_KIND_LABEL = "connection_action, cli, instruction, native_tool, script"
# The one top-level frontmatter block key readers read, and only for lesson data
# (``lesson``, ``capture``). Handler and workflow data live in ``corsia.yaml``; a
# non-lesson block under this key, or any CORSIA block under another key, is a
# validation error, never silently dropped metadata.
FRONTMATTER_KEY = "corsia"
# The CORSIA block shapes that identify one wherever it is filed: each field with the
# key it always carries (``handler.kind``, ``workflow.steps``).
_BLOCK_SIGNATURES = (("handler", "kind"), ("workflow", "steps"))
_SKILL_DIR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


@dataclass(frozen=True)
class SkillValidation:
    ok: bool
    name: str | None
    description: str | None
    metadata: dict[str, Any]
    body: str
    errors: list[str] = field(default_factory=list)
    # The CORSIA data (handler, workflow): the sidecar's, or a lesson block's.
    corsia: dict[str, Any] | None = None


def _lesson_block(meta: dict[str, Any]) -> dict[str, Any] | None:
    """The top-level block when it holds lesson data, the only block still read."""
    block = meta.get(FRONTMATTER_KEY)
    if isinstance(block, dict) and set(BLOCK_ONLY_KEYS) & set(block):
        return block
    return None


def _validate_corsia(meta: dict[str, Any]) -> list[str]:
    """Validate the top-level block: a lesson block as data, any other as retired."""
    corsia = meta.get(FRONTMATTER_KEY)
    if corsia is None:
        return []
    if not isinstance(corsia, dict):
        return ["corsia must be a mapping"]
    pointed = pointer_errors(meta.get("metadata"))[0]
    if _lesson_block(meta) is None:
        return [_retired_block_error(pointed)]
    if pointed:
        return [
            f"a top-level {FRONTMATTER_KEY} block holding lesson data cannot also have a "
            f"metadata.corsia-data pointer; a lesson skill keeps its CORSIA data in the block"
        ]
    return _validate_corsia_data(corsia)


def _retired_block_error(pointed: bool) -> str:
    if pointed:
        return (
            f"a top-level {FRONTMATTER_KEY} block and a metadata.corsia-data pointer both "
            f"declare CORSIA data; keep only the {SIDECAR_FILE} data"
        )
    return (
        f"the top-level {FRONTMATTER_KEY} block is retired for handler and workflow data; "
        f"move it to {SIDECAR_FILE} beside SKILL.md and point at it with "
        "metadata: {corsia-data: corsia.yaml, corsia-format: '1'}"
    )


def _validate_corsia_data(corsia: dict[str, Any]) -> list[str]:
    """Validate CORSIA data wherever it is filed: a lesson block or the sidecar."""
    errors: list[str] = []
    handler = corsia.get("handler")
    if handler is not None:
        if not isinstance(handler, dict):
            errors.append("corsia.handler must be a mapping")
        else:
            kind = handler.get("kind")
            if kind not in ALLOWED_HANDLER_KINDS:
                errors.append(f"corsia.handler.kind must be one of {_HANDLER_KIND_LABEL}")
    for key in ("inputs", "outputs", "events", "artifacts", "sandbox"):
        if key in corsia and not isinstance(corsia[key], (dict, list, str)):
            errors.append(f"corsia.{key} must be a mapping, list, or string")
    if "timeout" in corsia and not isinstance(corsia["timeout"], (int, float, str)):
        errors.append("corsia.timeout must be a number or duration string")
    if "model_profile" in corsia and not text_value(corsia["model_profile"]):
        errors.append("corsia.model_profile must be a non-empty string")
    return errors


def _misfiled_block_errors(meta: dict[str, Any]) -> list[str]:
    """Report a CORSIA block under another key, which no reader reads, as an error."""
    return [
        f"{key}.{block_field} is CORSIA metadata, which no reader reads under {key}; "
        f"move it to {SIDECAR_FILE} beside SKILL.md"
        for key, block in meta.items()
        if key != FRONTMATTER_KEY and isinstance(block, dict)
        for block_field, signature in _BLOCK_SIGNATURES
        if isinstance(block.get(block_field), dict) and signature in block[block_field]
    ]


def validate_skill_text(text: str) -> SkillValidation:
    """Validate one ``SKILL.md`` body alone: no filesystem, and no sidecar is consulted.

    Use :func:`validate_skill_bundle` or :func:`validate_skill_file` for a skill that may
    point at a ``corsia.yaml``.
    """
    return _validate(text, bundle=False)


def validate_skill_bundle(
    skill_md: str, files: Mapping[str, str] | None, *, sidecar_problem: str | None = None,
) -> SkillValidation:
    """Validate a ``SKILL.md`` with the supplied folder files; no filesystem access.

    ``files`` maps folder-relative names to their text; only ``corsia.yaml`` is read.
    ``sidecar_problem`` is the caller's :func:`read_sidecar` problem, reported only when
    the skill points at a sidecar, exactly as :func:`validate_skill_file` reports it.
    """
    sidecar = (files or {}).get(SIDECAR_FILE)
    return _validate(skill_md, bundle=True, sidecar=sidecar, problem=sidecar_problem)


def _sidecar_corsia(
    meta: dict[str, Any], sidecar: str | None, problem: str | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Resolve a skill's CORSIA data: the sidecar when pointed at, else a lesson block's."""
    lesson = _lesson_block(meta)
    declared, errors = pointer_errors(meta.get("metadata"))
    if not declared:
        return lesson, []
    if errors:
        return lesson, errors
    if problem is not None:
        return lesson, [problem]
    if sidecar is None:
        return lesson, [f"metadata.corsia-data names {SIDECAR_FILE}, which is not in the skill folder"]
    data, errors = parse_sidecar(sidecar)
    if data is None:
        return lesson, errors
    return data, _validate_corsia_data(data) + validate_workflow(data)


def _validate(
    text: str, *, bundle: bool, sidecar: str | None = None, problem: str | None = None,
) -> SkillValidation:
    try:
        meta, body = parse_frontmatter(text)
    except Exception as exc:  # noqa: BLE001 - any parse failure is user-facing.
        return SkillValidation(
            ok=False,
            name=None,
            description=None,
            metadata={},
            body=text,
            errors=[f"frontmatter parse failed: {exc}"],
        )

    errors: list[str] = []
    name = text_value(meta.get("name"))
    description = text_value(meta.get("description"))
    if not name:
        errors.append("name is required")
    if not description:
        errors.append("description is required")
    errors.extend(_validate_corsia(meta))
    errors.extend(_misfiled_block_errors(meta))
    data = _lesson_block(meta)
    if data is not None:
        errors.extend(validate_workflow(data))
        if LESSON_KEY in data:
            errors.extend(lesson_errors(data[LESSON_KEY], description, body))
    if bundle:
        data, sidecar_errors = _sidecar_corsia(meta, sidecar, problem)
        errors.extend(error for error in sidecar_errors if error not in errors)
    return SkillValidation(
        ok=not errors,
        name=name,
        description=description,
        metadata=meta,
        body=body,
        errors=errors,
        corsia=data,
    )


def frontmatter_span(text: str) -> tuple[int, int] | None:
    """Return the ``[start, end)`` span of the raw YAML frontmatter, or None without one."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        raise ValueError("missing closing frontmatter marker")
    return 4, end


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    span = frontmatter_span(text)
    if span is None:
        return {}, text
    start, end = span
    raw_meta = text[start:end]
    body_start = end + len("\n---")
    if text[body_start:body_start + 1] == "\n":
        body_start += 1
    import yaml

    parsed = yaml.safe_load(raw_meta) if raw_meta.strip() else {}
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise ValueError("frontmatter must be a mapping")
    return dict(parsed), text[body_start:]


def declares_sidecar(skill_md: str) -> bool:
    """True when the frontmatter declares a ``corsia.yaml`` pointer; unreadable text declares none."""
    import yaml

    try:
        return pointer_errors(parse_frontmatter(skill_md)[0].get("metadata"))[0]
    except (ValueError, yaml.YAMLError):  # validation reports an unparseable frontmatter.
        return False


def is_skill_file(path: Path) -> bool:
    """True when ``path`` is a standard ``skills/<skill>/SKILL.md`` file."""
    parts = path.parts
    return path.name == "SKILL.md" and len(parts) >= 3 and parts[-3] == "skills"


def validate_skill_file(path: Path) -> SkillValidation:
    """Validate a skill folder's ``SKILL.md`` with the ``corsia.yaml`` beside it.

    Read and parse errors are reported as validation errors.
    """
    try:
        text = path.read_text(encoding="utf-8")
        sidecar, problem = read_sidecar(path.parent)
        return _validate(text, bundle=True, sidecar=sidecar, problem=problem)
    except OSError as exc:
        return SkillValidation(
            ok=False,
            name=None,
            description=None,
            metadata={},
            body="",
            errors=[f"skill file unreadable: {exc}"],
        )


def validate_skill_dir_name(name: str) -> str:
    """Return a filesystem-safe skill folder/name or raise ValueError."""
    candidate = name.strip()
    if not _SKILL_DIR_RE.fullmatch(candidate):
        raise ValueError(
            "skill name must start with a letter or number and contain only "
            "letters, numbers, dash, underscore, or dot"
        )
    return candidate


def skill_template(name: str, description: str) -> str:
    """Standards-compatible starting SKILL.md content."""
    safe_name = validate_skill_dir_name(name)
    desc = text_value(description)
    if not desc:
        raise ValueError("description is required")
    import yaml

    frontmatter = yaml.safe_dump(
        {"name": safe_name, "description": desc},
        sort_keys=False,
        allow_unicode=False,
    )
    return (
        "---\n"
        f"{frontmatter}"
        "---\n"
        f"# {safe_name}\n\n"
        "Describe when and how an agent should use this skill.\n"
    )
