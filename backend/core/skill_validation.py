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
from core.skill_sidecar import SIDECAR_FILE, parse_sidecar, pointer_errors, read_sidecar

ALLOWED_HANDLER_KINDS = {
    "instruction",
    "cli",
    "native_tool",
    "connection_action",
    "script",
}
_HANDLER_KIND_LABEL = "connection_action, cli, instruction, native_tool, script"
_WORKFLOW_STEP_KEYS = frozenset(
    {
        "id",
        "title",
        "required",
        "check",
        "difficulty",
        "quality",
        "after",
        "replaces",
    }
)
_WORKFLOW_DIFFICULTIES = frozenset({"routine", "standard", "hard"})
# The one frontmatter block key every reader reads. A CORSIA block filed under any
# other key is a validation error, never silently dropped metadata.
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
    # The CORSIA data (handler, workflow), read from the sidecar or the legacy block.
    corsia: dict[str, Any] | None = None


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _workflow_step_shape_errors(step: dict[str, Any], label: str) -> list[str]:
    """Validate workflow fields whose values have a documented v1 shape."""
    errors: list[str] = []
    for key in step:
        if key == "models":
            errors.append(f"{label}.models is reserved and not allowed in v1")
        elif key not in _WORKFLOW_STEP_KEYS:
            errors.append(f"{label}.{key} is an unknown key")
    difficulty = step.get("difficulty")
    if difficulty is not None:
        if not isinstance(difficulty, dict):
            errors.append(f"{label}.difficulty must be a mapping")
        elif "default" in difficulty and difficulty["default"] not in _WORKFLOW_DIFFICULTIES:
            errors.append(
                f"{label}.difficulty.default must be one of routine, standard, hard"
            )
    quality = step.get("quality")
    if quality is not None and (
        not isinstance(quality, list) or any(not isinstance(item, dict) for item in quality)
    ):
        errors.append(f"{label}.quality must be a list of mappings")
    return errors


def _validate_corsia(meta: dict[str, Any]) -> list[str]:
    corsia = meta.get(FRONTMATTER_KEY)
    if corsia is None:
        return []
    if not isinstance(corsia, dict):
        return ["corsia must be a mapping"]
    return _validate_corsia_data(corsia)


def _validate_corsia_data(corsia: dict[str, Any]) -> list[str]:
    """Validate CORSIA data wherever it is filed: the legacy block or the sidecar."""
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
    if "model_profile" in corsia and not _text(corsia["model_profile"]):
        errors.append("corsia.model_profile must be a non-empty string")
    return errors


def _misfiled_block_errors(meta: dict[str, Any]) -> list[str]:
    """Report a CORSIA block under another key, which no reader reads, as an error."""
    return [
        f"{key}.{block_field} is CORSIA metadata, which is read only under "
        f"{FRONTMATTER_KEY}; move the block there"
        for key, block in meta.items()
        if key != FRONTMATTER_KEY and isinstance(block, dict)
        for block_field, signature in _BLOCK_SIGNATURES
        if isinstance(block.get(block_field), dict) and signature in block[block_field]
    ]


def _validate_workflow(corsia: dict[str, Any]) -> list[str]:
    """Validate checkable workflow steps without prescribing their full shape."""

    workflow = corsia.get("workflow")
    if workflow is None:
        return []
    if not isinstance(workflow, dict):
        return ["corsia.workflow must be a mapping"]
    steps = workflow.get("steps")
    if steps is None:
        return []
    if not isinstance(steps, list):
        return ["corsia.workflow.steps must be a list"]

    errors: list[str] = []
    known: set[str] = set()
    after_by_id: dict[str, list[str]] = {}
    for index, step in enumerate(steps):
        prefix = f"corsia.workflow.steps[{index}]"
        if not isinstance(step, dict):
            errors.append(f"{prefix} must be a mapping")
            continue
        step_id = _text(step.get("id"))
        step_label = f"{prefix} {step_id!r}" if step_id else prefix
        errors.extend(_workflow_step_shape_errors(step, step_label))
        if not step_id:
            errors.append(f"{prefix}.id is required")
            continue
        if step_id in known:
            errors.append(f"{prefix}.id duplicates {step_id!r}")
            continue
        known.add(step_id)
        if step.get("required") is True and not _text(step.get("check")):
            errors.append(f"{prefix}.check is required when required is true")
        after = step.get("after", [])
        if not isinstance(after, list) or any(not _text(value) for value in after):
            errors.append(f"{prefix}.after must be a list of step ids")
            continue
        after_by_id[step_id] = [str(value).strip() for value in after]

    for step_id, after in after_by_id.items():
        for dependency in after:
            if dependency not in known:
                errors.append(
                    f"corsia.workflow.steps {step_id!r}.after references unknown "
                    f"step {dependency!r}"
                )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(step_id: str, trail: list[str]) -> None:
        if step_id in visited:
            return
        if step_id in visiting:
            cycle = trail[trail.index(step_id):] + [step_id]
            errors.append("corsia.workflow.steps has a cycle: " + " -> ".join(cycle))
            return
        visiting.add(step_id)
        for dependency in after_by_id.get(step_id, []):
            if dependency in known:
                visit(dependency, [*trail, step_id])
        visiting.remove(step_id)
        visited.add(step_id)

    for step_id in after_by_id:
        visit(step_id, [])
    return errors


def validate_skill_text(text: str) -> SkillValidation:
    """Validate one ``SKILL.md`` body alone: no filesystem, and no sidecar is consulted.

    Use :func:`validate_skill_bundle` or :func:`validate_skill_file` for a skill that may
    point at a ``corsia.yaml``.
    """
    return _validate(text, bundle=False)


def validate_skill_bundle(skill_md: str, files: Mapping[str, str] | None) -> SkillValidation:
    """Validate a ``SKILL.md`` with the supplied folder files; no filesystem access.

    ``files`` maps folder-relative names to their text; only ``corsia.yaml`` is read.
    """
    return _validate(skill_md, bundle=True, sidecar=(files or {}).get(SIDECAR_FILE))


def _sidecar_corsia(
    meta: dict[str, Any], sidecar: str | None, problem: str | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Resolve a skill's CORSIA data: the sidecar when pointed at, else the legacy block."""
    block = meta.get(FRONTMATTER_KEY)
    legacy = block if isinstance(block, dict) else None
    declared, errors = pointer_errors(meta.get("metadata"))
    if not declared:
        return legacy, []
    if errors:
        return legacy, errors
    if problem is not None:
        return legacy, [problem]
    if sidecar is None:
        return legacy, [f"metadata.corsia-data names {SIDECAR_FILE}, which is not in the skill folder"]
    data, errors = parse_sidecar(sidecar)
    if data is None:
        return legacy, errors
    errors = _validate_corsia_data(data) + _validate_workflow(data)
    if FRONTMATTER_KEY in meta and block != data:
        errors.append(
            f"a top-level {FRONTMATTER_KEY} block and a metadata.corsia-data pointer both "
            f"declare CORSIA data, and they differ; keep only the {SIDECAR_FILE} data"
        )
    return data, errors


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
    name = _text(meta.get("name"))
    description = _text(meta.get("description"))
    if not name:
        errors.append("name is required")
    if not description:
        errors.append("description is required")
    errors.extend(_validate_corsia(meta))
    errors.extend(_misfiled_block_errors(meta))
    corsia = meta.get(FRONTMATTER_KEY)
    if isinstance(corsia, dict):
        errors.extend(_validate_workflow(corsia))
        if LESSON_KEY in corsia:
            errors.extend(lesson_errors(corsia[LESSON_KEY], description, body))
    data = corsia if isinstance(corsia, dict) else None
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
    desc = _text(description)
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
