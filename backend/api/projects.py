"""Project API routes — CRUD, summaries, and management."""

from __future__ import annotations

import json
from pathlib import Path
from infra.time import utc_now_iso

from fastapi import APIRouter, HTTPException

from infra.auth import authenticate
from infra import config
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db, run_read
from infra.conductor import conductor_title
from infra.thread_titles import retitle_thread
from infra.queries import get_project as _get_project
from infra.repo_policy_transition import RepoPolicyTransitionBusy
from api.project_events import notify_projects_changed
from api.project_types import (
    PROJECT_TRANSITIONS,
    VALID_PROJECT_STATUSES,
    ProjectCreate,
    ProjectTransition,
    ProjectUpdate,
    validate_accent_color as _validate_accent_color,
)

from api.project_scaffolding import (  # noqa: F401
    compute_doc_roots,
    initialize_project,
)
from core.project_context import project_context_filenames, resolve_project_paths
from infra.workspace_paths import WorkspaceReference, looks_like_workspace_reference

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _project_api_projection(project, *, db) -> dict:
    """Project a logical stored planning reference as an absolute client path."""
    projected = dict(project)
    if not looks_like_workspace_reference(projected.get("path")):
        return projected
    _name, planning_root = resolve_project_paths(
        config.WORKSPACE,
        str(projected["id"]),
        db=db,
    )
    if planning_root is None:
        raise ValueError(f"Project {projected['id']} planning root is unresolved")
    projected["path"] = str(planning_root)
    return projected


@router.get("")
async def list_projects(token: str, status: str = "active"):
    """List the project catalog, filtered by status ("all" for every project)."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    def _query():
        db = get_db()

        if status == "all":
            projects = db.execute("SELECT * FROM projects ORDER BY name").fetchall()
        else:
            projects = db.execute(
                "SELECT * FROM projects WHERE status = ? ORDER BY name", (status,)
            ).fetchall()

        result = [_project_api_projection(p, db=db) for p in projects]
        db.close()
        return result

    result = await run_read(_query)
    return {"projects": result, "count": len(result)}


@router.get("/{project_id}")
async def get_project(token: str, project_id: str):
    """Get project details with recent conversations."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    def _query():
        db = get_db()
        project = _get_project(project_id, db=db)
        if not project:
            db.close()
            return None

        conversations = db.execute(
            "SELECT id, title, model, created_at, updated_at FROM threads "
            "WHERE project_id = ? AND spawned_by IS NULL "
            "ORDER BY updated_at DESC LIMIT 20",
            (project_id,),
        ).fetchall()

        _name, workspace_dir = resolve_project_paths(
            config.WORKSPACE,
            project_id,
            db=db,
        )
        context_files = project_context_filenames(workspace_dir) if workspace_dir else []
        projected = _project_api_projection(project, db=db)

        db.close()

        return {
            **projected,
            "projects_root": str(
                (config.WORKSPACE / "projects").resolve(strict=False)
            ),
            "context_files": context_files,
            "conversations": [dict(c) for c in conversations],
        }

    result = await run_in_db(_query)
    if result is None:
        raise HTTPException(404, "Project not found")
    return result


@router.post("")
async def create_project(token: str, body: ProjectCreate):
    """Create a project row, workspace scaffold, and registered doc roots."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    if body.status not in VALID_PROJECT_STATUSES:
        raise HTTPException(
            400,
            f"Invalid status '{body.status}'. Must be one of: {sorted(VALID_PROJECT_STATUSES)}"
        )
    _validate_accent_color(body.accent_color)

    repo_path = body.repo_path or body.path or None

    def _create():
        db = get_db()
        try:
            existing = db.execute(
                "SELECT id FROM projects WHERE name = ?", (body.name,)
            ).fetchone()
            if existing:
                return "duplicate", None
            from infra.conductor import human_conductor_model

            project = initialize_project(
                db,
                name=body.name,
                description=body.description,
                status=body.status,
                repo_path_str=repo_path,
                accent_color=body.accent_color,
                user_id=user["id"],
                conductor_model=human_conductor_model(db),
            )
            return "ok", _project_api_projection(project, db=db)
        finally:
            db.close()

    try:
        status, project_dict = await run_in_db(_create)
    except RepoPolicyTransitionBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    if status == "duplicate":
        raise HTTPException(409, f"Project '{body.name}' already exists")
    notify_projects_changed(user["id"])

    return {
        "created": True,
        "project": project_dict,
        "workspace_path": project_dict.get("path"),
    }


def _rename_project_conductor(db, project, updates):
    if 'name' not in updates or updates['name'] == project['name'] or not project['conductor_thread_id']:
        return
    before = db.execute('SELECT * FROM threads WHERE id = ?', (project['conductor_thread_id'],)).fetchone()
    if before and before['title'] == conductor_title(project['name']):
        title = conductor_title(updates['name'])
        retitle_thread(db, before, title, updates['updated_at'])
        return before['id'], title
    return None


def _notify_project_update(user_id, renamed, updated_at):
    notify_projects_changed(user_id)
    if renamed:
        from infra import state
        thread_id, title = renamed
        state.broadcast_event(thread_id, user_id, {
            'type': 'session_updated', 'thread_id': thread_id, 'title': title,
            'updated_at': updated_at,
        })


@router.patch("/{project_id}")
async def update_project(token: str, project_id: str, body: ProjectUpdate):
    """Update project fields."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    updates: dict = {}
    if body.name is not None:
        updates["name"] = body.name
    if body.description is not None:
        updates["description"] = body.description
    if "accent_color" in body.model_fields_set:
        _validate_accent_color(body.accent_color)
        updates["accent_color"] = body.accent_color
    new_project_path = None
    if body.path is not None:
        candidate_path = Path(body.path).expanduser()
        projects_root = (config.WORKSPACE / "projects").resolve(strict=False)
        new_project_path = candidate_path.resolve()
        if (
            candidate_path.is_symlink()
            or not new_project_path.is_dir()
            or new_project_path.parent != projects_root
        ):
            raise HTTPException(
                400,
                "Project path must be an existing folder directly inside the projects folder",
            )
        updates["path"] = WorkspaceReference.from_path(
            new_project_path,
            config.WORKSPACE_SELECTION,
        ).serialize()
        updates["doc_roots"] = json.dumps(compute_doc_roots(new_project_path))
    if body.status is not None:
        if body.status not in VALID_PROJECT_STATUSES:
            raise HTTPException(400, f"Status must be one of: {sorted(VALID_PROJECT_STATUSES)}")
        updates["status"] = body.status

    set_bootloader = "bootloader_files" in body.model_fields_set
    if set_bootloader:
        raise HTTPException(
            400,
            "Project context is directory-backed; manage Markdown files in context/",
        )

    if not updates:
        raise HTTPException(400, "No fields to update")

    def _update():
        with write_transaction(None, scope="project_update") as db:
            project = _get_project(project_id, db=db)
            if not project:
                return None

            if "name" in updates:
                clash = db.execute(
                    "SELECT id FROM projects WHERE name = ? AND id != ?",
                    (updates["name"], project_id),
                ).fetchone()
                if clash:
                    return "duplicate"

            updates["updated_at"] = utc_now_iso()
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [project_id]
            db.execute(f"UPDATE projects SET {set_clause} WHERE id = ?", values)
            renamed = _rename_project_conductor(db, project, updates)

        return _project_api_projection(
            _get_project(project_id, db=db),
            db=db,
        ), renamed

    result = await run_in_db(_update)
    if result is None:
        raise HTTPException(404, "Project not found")
    if result == "duplicate":
        raise HTTPException(409, f"A project named '{updates['name']}' already exists")
    _notify_project_update(user['id'], result[1], updates['updated_at'])
    return {"ok": True, "project": result[0]}


@router.post("/{project_id}/transition")
async def transition_project(token: str, project_id: str, body: ProjectTransition):
    """Transition a project through its lifecycle stages."""
    user = authenticate(token)
    if not user:
        raise HTTPException(401)

    if body.to_status not in VALID_PROJECT_STATUSES:
        raise HTTPException(
            400,
            f"Invalid status '{body.to_status}'. Must be one of: {sorted(VALID_PROJECT_STATUSES)}"
        )

    def _transition():
        with write_transaction(None, scope="project_transition") as db:
            project = _get_project(project_id, db=db)
            if not project:
                return "not_found", None, None

            current_status = project["status"]
            allowed = PROJECT_TRANSITIONS.get(current_status, [])

            if body.to_status not in allowed:
                return "invalid_transition", current_status, allowed

            now = utc_now_iso()
            db.execute(
                "UPDATE projects SET status = ?, updated_at = ? WHERE id = ?",
                (body.to_status, now, project_id),
            )

        project = _get_project(project_id, db=db)
        projected = _project_api_projection(project, db=db)

        return "ok", current_status, projected

    status, current_status, data = await run_in_db(_transition)
    if status == "not_found":
        raise HTTPException(404, "Project not found")
    if status == "invalid_transition":
        raise HTTPException(
            400,
            f"Cannot transition project from '{current_status}' to '{body.to_status}'. "
            f"Allowed transitions from '{current_status}': {data}"
        )

    return {
        "transitioned": True,
        "project_id": project_id,
        "from_status": current_status,
        "to_status": body.to_status,
        "project": data,
    }
