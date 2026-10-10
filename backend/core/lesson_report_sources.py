"""Read report source inputs without materializing or recording context delivery."""
from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any

from core.lesson_files import CAPTURE_DIR
from core.lesson_report_inventory import source_record
from core.project_context import resolve_project_row_paths
from core.role_context_sources import ROLE_ORDER, RoleScopeRoot, role_context_sources
from core.thread_identity import thread_agent_identity
from core.skill_scanning import skill_files
from core.system_tier import system_tier_manifest
from core.workspace_layout import ThreadRootResolver, resolve_workspace_root
from infra.conductor import workspace_conductor_id
from infra.guidance_git import MAX_GUIDANCE_BYTES

SOURCE_DISCOVERY_ISSUE = "Read-only guidance source discovery (legacy issue #3364)"


def _source_rows(db, thread_ids: tuple[str, ...], user_id: str):
    """Read the complete scoped ancestry and role facts in one statement."""
    marks = ", ".join("?" for _ in thread_ids)
    return db.execute(
        f"""
        WITH RECURSIVE
        selected(root_id) AS (
            SELECT id FROM threads WHERE id IN ({marks}) AND user_id = ?
        ),
        ancestry(root_id, id) AS (
            SELECT root_id, root_id FROM selected
            UNION
            SELECT ancestry.root_id, COALESCE(thread.spawned_by, thread.parent_thread_id)
            FROM ancestry
            JOIN threads AS thread ON thread.id = ancestry.id
            WHERE COALESCE(thread.spawned_by, thread.parent_thread_id) IS NOT NULL
        )
        SELECT ancestry.root_id, thread.*, root.project_id AS root_project_id,
               project.name AS root_project_name, project.path AS root_project_path,
               EXISTS(
                   SELECT 1 FROM projects AS conductor_project
                   WHERE conductor_project.conductor_thread_id = root.id
               ) AS root_is_project_conductor,
               (
                   SELECT value FROM settings
                   WHERE key = 'workspace_conductor_session_id'
               ) AS workspace_conductor_value
        FROM ancestry
        JOIN threads AS thread ON thread.id = ancestry.id
        JOIN threads AS root ON root.id = ancestry.root_id
        LEFT JOIN projects AS project ON project.id = root.project_id
        """,
        (*thread_ids, user_id),
    ).fetchall()


class _ProjectRoots:
    """Resolve each distinct project's planning root, and its canonical form, once."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self._planning: dict[tuple[Any, Any], Path | None] = {}
        self._canonical: dict[Path, Path] = {}

    def planning(self, name: Any, raw_path: Any) -> Path | None:
        key = (name, raw_path)
        if key not in self._planning:
            self._planning[key] = resolve_project_row_paths(
                self._workspace, project_name=name, raw_path=raw_path,
            )[1]
        return self._planning[key]

    def canonical(self, planning: Path) -> Path:
        if planning not in self._canonical:
            self._canonical[planning] = planning.resolve(strict=False)
        return self._canonical[planning]


def source_inputs(db, workspace: Path, scope) -> tuple[dict, list, list]:
    """Collect scoped thread, project, ancestry, and role facts in one query.

    File discovery remains outside the snapshot.  The recursive query keeps
    ancestry discovery from becoming one thread/project/role query per scope
    member; the Python projection deliberately retains the established
    same-user and parent-before-child semantics.
    """
    workspace_root = resolve_workspace_root(workspace)
    roots = {workspace_root: {"scope": "workspace", "scope_id": "workspace"}}
    roles: list[tuple[str, tuple, tuple[RoleScopeRoot, ...]]] = []
    unavailable: list[dict[str, str]] = []
    thread_ids = tuple(dict.fromkeys(scope.thread_ids))
    if not thread_ids:
        return roots, roles, unavailable
    rows = _source_rows(db, thread_ids, scope.user_id)
    by_root: dict[str, dict[str, Any]] = {}
    for row in rows:
        root_id = str(row["root_id"])
        grouped = by_root.setdefault(root_id, {})
        grouped[str(row["id"])] = row
    workspace_conductor = (
        workspace_conductor_id({"value": rows[0]["workspace_conductor_value"]})
        if rows and rows[0]["workspace_conductor_value"] is not None
        else None
    )
    # Each filesystem call releases the GIL, so a project's planning root and a thread's
    # directory are resolved once per report, never once per scoped thread.
    thread_roots = ThreadRootResolver()
    project_roots = _ProjectRoots(workspace)
    for thread in thread_ids:
        entries = by_root.get(thread)
        if not entries:
            continue
        root_row = entries[thread]
        project_id = root_row["root_project_id"]
        planning = None
        if project_id:
            planning = project_roots.planning(root_row["root_project_name"], root_row["root_project_path"])
        if planning:
            roots[planning] = {"scope": "project", "scope_id": project_id}
        if not project_id or planning is not None:
            chain = []
            current = thread
            while current and current not in chain and current in entries:
                chain.append(current)
                row = entries[current]
                current = row["spawned_by"] or row["parent_thread_id"]
            for ancestor in reversed(chain):
                row = entries[ancestor]
                if row["user_id"] == scope.user_id:
                    roots[thread_roots.root(planning or workspace, ancestor)] = {
                        "scope": "thread", "scope_id": ancestor,
                    }
        else:
            unavailable.append({"path": f"thread://{thread}",
                                "reason": "project planning root unavailable; selected thread and ancestor file roots unavailable"})
        try:
            is_agent, _is_subagent, _title, _responsibility = thread_agent_identity(root_row)
            if is_agent is None:
                raise sqlite3.OperationalError("Agent identity unavailable: threads.agent_enabled missing")
        except (sqlite3.Error, KeyError) as exc:
            unavailable.append({"path": f"thread://{thread}/roles",
                                "reason": f"role source resolution unavailable: {type(exc).__name__}"})
            continue
        is_conductor = bool(root_row["root_is_project_conductor"]) or thread == workspace_conductor
        selected_roles = ROLE_ORDER if is_conductor else (("agent",) if is_agent else ())
        role_roots = [RoleScopeRoot("workspace", "workspace", workspace_root)]
        if project_id and planning is not None:
            role_roots.append(RoleScopeRoot("project", str(project_id), project_roots.canonical(planning)))
        roles.append((thread, selected_roles, tuple(role_roots)))
    return roots, roles, unavailable


def current_sources(workspace: Path, inputs: tuple[dict, list, list]) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict]:
    """Scan current files after closing the snapshot; no receipt path grants access."""
    roots, roles, missing_roots = inputs
    root = resolve_workspace_root(workspace).resolve()
    paths = {}
    unavailable = list(missing_roots)
    role_files = set()
    for candidate, source_scope in roots.items():
        for path in [*skill_files(candidate), *candidate.joinpath(*CAPTURE_DIR).glob("*.md")]:
            paths[path] = source_scope
    for thread, selected_roles, role_roots in roles:
        for source in role_context_sources(selected_roles, role_roots, thread_id=thread):
            if source.inclusion == "omitted":
                unavailable.append({"path": source.stable_reference, "reason": source.reason or "role context unavailable"})
                continue
            path = Path(source.stable_reference)
            keys = ("scope", "scope_id", "role")
            if any(key not in source.metadata for key in keys):
                unavailable.append({"path": str(path), "reason": "role source scope metadata unavailable"})
                continue
            role_files.add(path)
            paths[path] = {key: source.metadata[key] for key in keys}
    sources = []
    for path in sorted(paths):
        try:
            if not path.resolve().is_relative_to(root):
                raise ValueError("source is outside the workspace")
            if path.stat().st_size > MAX_GUIDANCE_BYTES:
                raise ValueError("source exceeds guidance limit")
            record = source_record(path.read_text(encoding="utf-8"), str(path.resolve()))
        except (OSError, UnicodeError, ValueError) as exc:
            unavailable.append({"path": str(path), "reason": type(exc).__name__})
            continue
        if record is not None:
            sources.append(record | {"source_scope": paths[path]})
    try:
        manifest = system_tier_manifest()
        system = {"packaged_version": manifest.version, "declared_files": len(manifest.files),
                  "mismatches": [{"path": item.path, "expected_sha256": item.expected_sha256,
                                  "actual_sha256": item.actual_sha256} for item in manifest.mismatches]}
    except (OSError, ValueError) as exc:
        system = {"packaged_version": None, "declared_files": None, "reason": type(exc).__name__}
    system |= {"projection_status": None,
               "projection_reason": "pure workspace System projection reader unavailable; package availability is not recorded delivery"}
    discovery = {"system": system, "role_context_files": len(role_files),
                 "role_skill_rows": None,
                 "role_skill_reason": "pure role skill enumeration unavailable; delivery composer emits events",
                 "limitations_issue": SOURCE_DISCOVERY_ISSUE, "current_files_are_delivery": False}
    return sources, unavailable, discovery
