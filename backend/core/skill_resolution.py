"""Scope-root discovery and effective skill resolution."""

from __future__ import annotations

from pathlib import Path
import sqlite3

from loguru import logger

from core.legacy_context_origin import normalize_skill_scope as _legacy_normalize_skill_scope
from core.role_context_sources import RoleContextResolution, role_directory, resolve_role_context
from core.skill_registry_types import SkillResolution, SkillRow, SkillScope
from core.skill_scanning import (
    mark_duplicate_scope_errors,
    resolve_winners,
    role_skill_root_error,
    scan_scope,
)
from core.system_tier import system_skill_names
from infra.product_events import emit_product_event
from infra.system_package import SYSTEM_VERSION

_SYSTEM_UNAVAILABLE = "System skills unavailable"


def normalize_skill_scope(scope: str) -> SkillScope:
    """Normalize one skill scope, including the read-only System tier."""
    if scope == "system":
        return "system"
    return _legacy_normalize_skill_scope(scope)


def project_dir(project_id: str | None, workspace: Path, db) -> Path | None:
    """Resolve the project planning root when a project is available."""
    if not project_id or db is None:
        return None
    from core.project_context import resolve_project_paths

    try:
        _name, planning_root = resolve_project_paths(
            workspace,
            project_id,
            db=db,
        )
    except sqlite3.Error:
        return None
    return planning_root


def thread_dir(
    thread_id: str,
    *,
    project_dir: Path | None,
    workspace: Path,
) -> Path:
    """Return the canonical durable root for one thread."""
    from core.workspace_layout import resolve_thread_root

    return resolve_thread_root(project_dir or workspace, thread_id)


def thread_ancestry(thread_id: str, db) -> list[str]:
    """Return ancestors first so a later child skill overrides its parent."""
    if db is None:
        return [thread_id]
    chain: list[str] = []
    seen: set[str] = set()
    current: str | None = thread_id
    while current and current not in seen:
        seen.add(current)
        chain.append(current)
        try:
            row = db.execute(
                "SELECT spawned_by, parent_thread_id FROM threads WHERE id = ?",
                (current,),
            ).fetchone()
        except Exception:  # noqa: BLE001 - minimal DBs may omit ancestry columns.
            break
        if not row:
            break
        keys = row.keys() if hasattr(row, "keys") else ()
        spawned_by = row["spawned_by"] if "spawned_by" in keys else None
        parent = row["parent_thread_id"] if "parent_thread_id" in keys else None
        current = spawned_by or parent
    return list(reversed(chain))


def system_skills_root(workspace: Path) -> Path | None:
    """Return the verified agent-readable System projection, if available."""
    from core.system_projection import ensure_system_projection

    root = ensure_system_projection(workspace)
    return root if root and (root / "skills").is_dir() else None


def scope_roots(
    *,
    thread_id: str,
    project_id: str | None,
    workspace: Path,
    db,
) -> tuple[list[tuple[Path, SkillScope, str]], str | None]:
    """Return roots in their deliberate System-to-thread precedence order."""
    from core.workspace_layout import resolve_workspace_root

    system_dir = system_skills_root(workspace)
    system_reason = None if system_dir else _SYSTEM_UNAVAILABLE
    roots: list[tuple[Path, SkillScope, str]] = []
    if system_dir is not None:
        roots.append((system_dir, "system", SYSTEM_VERSION))
    roots.append((resolve_workspace_root(workspace), "workspace", "workspace"))
    resolved_project_dir = project_dir(project_id, workspace, db)
    if resolved_project_dir is not None:
        roots.append((resolved_project_dir, "project", project_id or "project"))
    if project_id and resolved_project_dir is None:
        return roots, system_reason
    for ancestor_id in thread_ancestry(thread_id, db):
        roots.append(
            (
                thread_dir(ancestor_id, project_dir=resolved_project_dir, workspace=workspace),
                "thread",
                ancestor_id,
            )
        )
    return roots, system_reason


def local_skill_inventory(
    *,
    scope: SkillScope,
    workspace: Path,
    db,
    project_id: str | None = None,
    thread_id: str | None = None,
) -> list[SkillRow]:
    """Return only skills physically present at one requested scope."""
    if scope == "system":
        root = system_skills_root(workspace)
        return scan_scope(root, "system", SYSTEM_VERSION) if root else []
    if scope == "workspace":
        from core.workspace_layout import resolve_workspace_root

        return mark_duplicate_scope_errors(
            scan_scope(resolve_workspace_root(workspace), "workspace", "workspace")
        )
    if scope == "project":
        resolved_project_dir = project_dir(project_id, workspace, db)
        return mark_duplicate_scope_errors(
            scan_scope(resolved_project_dir, "project", project_id or "project")
            if resolved_project_dir
            else []
        )
    if not thread_id:
        return []
    resolved_project_dir = project_dir(project_id, workspace, db)
    if project_id and resolved_project_dir is None:
        return []
    return mark_duplicate_scope_errors(
        scan_scope(
            thread_dir(thread_id, project_dir=resolved_project_dir, workspace=workspace),
            "thread",
            thread_id,
        )
    )


def _scan_role_roots(scope_root, roles) -> list[SkillRow]:
    """Scan each role's skill directory under one scope root."""
    rows: list[SkillRow] = []
    for role in roles:
        try:
            role_root = role_directory(scope_root.root, role)
        except RuntimeError as error:
            logger.warning("role skill root unavailable: {}", error)
            rows.append(role_skill_root_error(scope_root, role, error))
            continue
        rows.extend(
            mark_duplicate_scope_errors(
                scan_scope(
                    role_root,
                    scope_root.scope,
                    scope_root.scope_id,
                    role=role,
                )
            )
        )
    return rows


def resolve_effective_skills(
    *,
    thread_id: str,
    project_id: str | None,
    workspace: Path,
    db,
    role_context: RoleContextResolution | None = None,
) -> SkillResolution:
    """Resolve System, workspace, project, role, and thread skills in order."""
    rows: list[SkillRow] = []
    roots, system_unavailable = scope_roots(
        thread_id=thread_id,
        project_id=project_id,
        workspace=workspace,
        db=db,
    )
    base_roots = [root for root in roots if root[1] != "thread"]
    thread_roots = [root for root in roots if root[1] == "thread"]
    resolution = role_context or resolve_role_context(
        thread_id=thread_id,
        project_id=project_id,
        workspace=workspace,
        db=db,
        load_sources=False,
    )
    role_roots_by_scope = {
        (scope_root.scope, scope_root.scope_id): scope_root
        for scope_root in resolution.scope_roots
    }
    for root, scope, scope_id in base_roots:
        rows.extend(mark_duplicate_scope_errors(scan_scope(root, scope, scope_id)))
        scope_root = role_roots_by_scope.get((scope, scope_id))
        if scope_root is not None:
            rows.extend(_scan_role_roots(scope_root, resolution.roles))

    for root, scope, scope_id in thread_roots:
        rows.extend(mark_duplicate_scope_errors(scan_scope(root, scope, scope_id)))

    errors = [row for row in rows if row.status == "error"]
    valid = [row for row in rows if row.status != "error"]
    included, omitted = resolve_winners(
        valid, system_skill_names() if system_unavailable else frozenset()
    )
    emit_product_event(
        "skill_registry",
        "resolved",
        thread_id=thread_id,
        properties={
            "project_id": project_id,
            "included": len(included),
            "omitted": len(omitted),
            "errors": len(errors),
        },
    )
    if errors:
        emit_product_event(
            "skill_registry",
            "validation_failed",
            thread_id=thread_id,
            properties={"error_count": len(errors)},
        )
    return SkillResolution(
        included=included,
        omitted=omitted,
        errors=errors,
        system_unavailable=system_unavailable,
    )


def skill_scope_root(
    *,
    scope: SkillScope,
    workspace: Path,
    db,
    project_id: str | None = None,
    thread_id: str | None = None,
) -> Path | None:
    """Return the filesystem root that owns one scope's ``skills/`` directory."""
    if scope == "system":
        return system_skills_root(workspace)
    if scope == "workspace":
        from core.workspace_layout import resolve_workspace_root

        return resolve_workspace_root(workspace)
    if scope == "project":
        return project_dir(project_id, workspace, db)
    if scope == "thread" and thread_id:
        resolved_project_dir = project_dir(project_id, workspace, db)
        if project_id and resolved_project_dir is None:
            return None
        return thread_dir(thread_id, project_dir=resolved_project_dir, workspace=workspace)
    return None
