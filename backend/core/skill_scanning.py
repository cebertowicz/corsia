"""Skill discovery, duplicate detection, and precedence helpers."""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from core.lesson_files import trial_from_validation
from core.role_context_sources import RoleName, RoleScopeRoot
from core.skill_registry_types import SkillRow, SkillScope
from core.skill_validation import validate_skill_file


def skill_files(root: Path) -> list[Path]:
    """Return direct ``skills/<name>/SKILL.md`` files in deterministic order."""
    skills_dir = root / "skills"
    if not skills_dir.is_dir():
        return []
    return sorted(
        path
        for path in skills_dir.glob("*/SKILL.md")
        if path.is_file() and path.parent.parent == skills_dir
    )


def scan_scope(
    root: Path,
    scope: SkillScope,
    scope_id: str,
    *,
    role: RoleName | None = None,
) -> list[SkillRow]:
    """Read and validate skills physically present at one scope root."""
    rows: list[SkillRow] = []
    for path in skill_files(root):
        validation = validate_skill_file(path)
        if validation.ok and validation.name and validation.description:
            rows.append(
                SkillRow(
                    name=validation.name,
                    description=validation.description,
                    scope=scope,
                    scope_id=scope_id,
                    path=path.resolve(strict=False),
                    body_path=path.resolve(strict=False),
                    metadata=validation.metadata,
                    status="included",
                    role=role,
                    corsia=validation.corsia,
                    trial=trial_from_validation(validation),
                    raw_sha256=validation.raw_sha256,
                    source_data=validation.source_data,
                )
            )
        else:
            rows.append(
                SkillRow(
                    name=validation.name or path.parent.name,
                    description=validation.description or "",
                    scope=scope,
                    scope_id=scope_id,
                    path=path.resolve(strict=False),
                    body_path=path.resolve(strict=False),
                    metadata=validation.metadata,
                    status="error",
                    errors=validation.errors,
                    role=role,
                    corsia=validation.corsia,
                )
            )
    return rows


def mark_duplicate_scope_errors(rows: list[SkillRow]) -> list[SkillRow]:
    """Move duplicate names within one scope and role into the error bucket."""
    by_key: dict[tuple[str, str, RoleName | None, str], list[SkillRow]] = {}
    for row in rows:
        if row.status == "error":
            continue
        by_key.setdefault(
            (row.scope, row.scope_id, row.role, row.name),
            [],
        ).append(row)
    duplicate_ids: set[tuple[str, str, RoleName | None, Path]] = set()
    for (scope, _scope_id, role, name), dupes in by_key.items():
        if len(dupes) <= 1:
            continue
        role_label = f" {role} role" if role else ""
        logger.warning(
            "duplicate skill name in {}{} scope: {}",
            scope,
            role_label,
            name,
        )
        duplicate_ids.update(
            (row.scope, row.scope_id, row.role, row.path) for row in dupes
        )
    marked: list[SkillRow] = []
    for row in rows:
        if (row.scope, row.scope_id, row.role, row.path) not in duplicate_ids:
            marked.append(row)
            continue
        marked.append(
            SkillRow(
                name=row.name,
                description=row.description,
                scope=row.scope,
                scope_id=row.scope_id,
                path=row.path,
                body_path=row.body_path,
                metadata=row.metadata,
                status="error",
                errors=[
                    "duplicate skill name in "
                    f"{row.scope}{' ' + row.role + ' role' if row.role else ''} "
                    f"scope: {row.name}"
                ],
                role=row.role,
                corsia=row.corsia,
                trial=row.trial,
                raw_sha256=row.raw_sha256,
                source_data=row.source_data,
            )
        )
    return marked


def role_skill_root_error(
    scope_root: RoleScopeRoot,
    role: RoleName,
    error: RuntimeError,
) -> SkillRow:
    """Expose an unavailable role skill root through the normal error surface."""
    path = scope_root.root / "context" / "roles" / role
    return SkillRow(
        name=f"{role}-role-skills",
        description="",
        scope=scope_root.scope,
        scope_id=scope_root.scope_id,
        path=path,
        body_path=path,
        metadata={"provider": "skills", "role": role},
        status="error",
        errors=[f"role skill root unavailable: {error}"],
        role=role,
    )


def omitted_row(row: SkillRow, reason: str, overrides: str | None = None) -> SkillRow:
    """Copy a row into the omitted bucket carrying its reason."""
    return SkillRow(
        name=row.name,
        description=row.description,
        scope=row.scope,
        scope_id=row.scope_id,
        path=row.path,
        body_path=row.body_path,
        metadata=row.metadata,
        status="omitted",
        errors=[reason],
        overrides=overrides,
        role=row.role,
        corsia=row.corsia,
        trial=row.trial,
        raw_sha256=row.raw_sha256,
        source_data=row.source_data,
    )


def resolve_winners(
    valid: list[SkillRow],
    unread_system_names: frozenset[str] = frozenset(),
) -> tuple[list[SkillRow], list[SkillRow]]:
    """Apply System shadowing then most-specific-scope-wins precedence."""
    winner_by_name: dict[str, SkillRow] = {}
    omitted: list[SkillRow] = []
    for row in valid:
        if row.name in unread_system_names:
            omitted.append(omitted_row(row, "shadowed by System"))
            continue
        previous = winner_by_name.get(row.name)
        if previous is None:
            winner_by_name[row.name] = row
            continue
        if previous.scope == "system":
            omitted.append(omitted_row(row, "shadowed by System"))
            continue
        omitted.append(
            omitted_row(
                previous,
                "overridden by a more specific skill",
                overrides=row.name,
            )
        )
        winner_by_name[row.name] = row
    return list(winner_by_name.values()), omitted
