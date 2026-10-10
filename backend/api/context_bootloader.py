"""CTX bootloader management API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from core.bootloader_management import (
    BootloaderTarget,
    create_bootloader_file,
    log_bootloader_event,
    normalize_bootloader_entries,
)
from core.project_context import project_context_filenames, resolve_project_paths
from core.workspace_layout import (
    resolve_project_root,
    resolve_thread_root,
    resolve_workspace_root,
)
from core.workspace_context_sources import workspace_context_configured_files
from infra.auth import require_user
from infra import config
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db
from infra.queries import get_thread_for_user
from infra.time import utc_now_iso

router = APIRouter(prefix="/api/context/bootloader", tags=["context"])


def _record_context_change(
    db,
    *,
    target: BootloaderTarget,
    action: str,
    files: list[str],
) -> None:
    from infra.passive_changes import append_change

    labels = {"workspace": "Workspace", "project": "Project", "thread": "Thread"}
    append_change(
        db,
        object_kind=f"{target.scope}_context",
        object_id=target.target_id,
        change_kind=action,
        summary=f"{labels[target.scope]} context updated",
        details={"scope": target.scope, "files": files},
        actor_thread_id=None,
    )


def _target_from_body(body: dict[str, Any], db, user_id: str) -> BootloaderTarget:
    if "actor_session_id" in body:
        raise HTTPException(422, "HTTP context changes cannot claim a task actor")
    scope = body.get("scope")
    if scope in {"global", "workspace"}:
        return BootloaderTarget(
            scope="workspace",
            target_id="workspace",
            root=config.WORKSPACE,
            allowed_roots=(config.WORKSPACE, config.WORKSPACE.parent),
            store_absolute=True,
        )
    if scope == "project":
        project_id = body.get("project_id") or body.get("target_id")
        if not project_id:
            raise HTTPException(400, "project_id required")
        _name, workspace_dir = resolve_project_paths(
            config.WORKSPACE,
            project_id,
            db=db,
        )
        if workspace_dir is None:
            raise HTTPException(400, f"Project working directory unresolved for project_id={project_id}")
        root = workspace_dir / "context"
        return BootloaderTarget(
            scope="project",
            target_id=str(project_id),
            root=root,
            allowed_roots=(root,),
            store_absolute=False,
        )
    if scope == "thread":
        thread_id = body.get("thread_id") or body.get("target_id")
        if not thread_id:
            raise HTTPException(400, "session_id required")
        thread_row = get_thread_for_user(str(thread_id), user_id, db=db)
        if not thread_row:
            raise HTTPException(404, "Thread not found")
        project_id = thread_row["project_id"] if "project_id" in thread_row.keys() else None
        workspace = resolve_workspace_root(config.WORKSPACE)
        scope_root = (
            resolve_project_root(workspace, project_id, db=db)
            if project_id
            else workspace
        )
        if scope_root is None:
            raise HTTPException(
                400,
                f"Project working directory unresolved for project_id={project_id}",
            )
        try:
            thread_root = resolve_thread_root(scope_root, str(thread_id))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        root = thread_root / "context"
        allowed = [workspace, scope_root, thread_root]
        return BootloaderTarget(
            scope="thread",
            target_id=str(thread_id),
            root=root,
            allowed_roots=tuple(allowed),
            store_absolute=True,
        )
    raise HTTPException(400, "scope must be workspace (or legacy global), project, or thread")


def _current_files(target: BootloaderTarget, db) -> list[str]:
    if target.scope == "workspace":
        return workspace_context_configured_files(db=db, workspace=config.WORKSPACE)
    if target.scope == "project":
        return project_context_filenames(target.root.parent)
    row = db.execute(
        "SELECT bootloader_files FROM threads WHERE id = ?",
        (target.target_id,),
    ).fetchone()
    raw = row["bootloader_files"] if row and row["bootloader_files"] is not None else None
    if raw is None:
        return []
    parsed = json.loads(raw)
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _persist_files(target: BootloaderTarget, files: list[str], db) -> None:
    now = utc_now_iso()
    if target.scope == "workspace":
        row = db.execute(
            "SELECT value FROM settings WHERE key = 'workspace_context'",
        ).fetchone()
        current = {}
        if row and row["value"]:
            try:
                parsed = json.loads(row["value"])
                if isinstance(parsed, dict):
                    current = parsed
            except (TypeError, json.JSONDecodeError):
                current = {}
        current["bootloader_files"] = files
        db.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value = excluded.value, updated_at = excluded.updated_at",
            ("workspace_context", json.dumps(current), now),
        )
        return
    if target.scope == "project":
        raise ValueError(
            "Project context is directory-backed; manage Markdown files in context/"
        )
    db.execute(
        "UPDATE threads SET bootloader_files = ?, updated_at = ? WHERE id = ?",
        (json.dumps(files), now, target.target_id),
    )


def _write_context_change(
    db,
    *,
    target: BootloaderTarget,
    user_id: str,
    action: str,
    files: list[str],
    expected_files: list[str] | None,
) -> None:
    """Commit one edit with current ownership and saved-list evidence."""
    with write_transaction(db, scope="context_bootloader_change") as connection:
        if target.scope == "thread" and not get_thread_for_user(
            target.target_id, user_id, db=connection,
        ):
            raise HTTPException(404, "Thread not found")
        if target.scope == "project":
            if not connection.execute(
                "SELECT id FROM projects WHERE id = ?", (target.target_id,),
            ).fetchone():
                raise ValueError(
                    f"Project working directory unresolved for project_id={target.target_id}"
                )
        else:
            if _current_files(target, connection) != expected_files:
                raise ValueError("Context changed while validating files. Refresh and try again.")
            _persist_files(target, files, connection)
        _record_context_change(connection, target=target, action=action, files=files)
        log_bootloader_event(
            connection,
            actor_user_id=user_id,
            scope=target.scope,
            target_id=target.target_id,
            action=action,
            files=files,
        )


@router.patch("")
async def update_bootloader_files(body: dict, user: dict = Depends(require_user)):
    """Replace one bootloader layer's included file list."""

    def _update():
        db = get_db()
        try:
            target = _target_from_body(body, db, user["id"])
            if target.scope == "project":
                raise ValueError(
                    "Project context is directory-backed; manage Markdown files in context/"
                )
            current = _current_files(target, db)
            files = normalize_bootloader_entries(
                body.get("files"),
                target,
                existing_entries=set(current),
            )
            _write_context_change(
                db,
                target=target,
                user_id=user["id"],
                action="replace",
                files=files,
                expected_files=current,
            )
            return {"scope": target.scope, "target_id": target.target_id, "files": files}
        finally:
            db.close()

    try:
        result = await run_in_db(_update)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    return {"ok": True, **result}


@router.post("/attach")
async def attach_bootloader_file(body: dict, user: dict = Depends(require_user)):
    """Attach one existing file to a workspace, project, or thread bootloader."""
    def _attach():
        db = get_db()
        try:
            target = _target_from_body(body, db, user["id"])
            if target.scope == "project":
                raise ValueError(
                    "Project context is directory-backed; create a Markdown file in context/"
                )
            current = _current_files(target, db)
            files = normalize_bootloader_entries(
                [*current, body.get("path")],
                target,
                existing_entries=set(current),
            )
            _write_context_change(
                db,
                target=target,
                user_id=user["id"],
                action="attach",
                files=files,
                expected_files=current,
            )
            return {"scope": target.scope, "target_id": target.target_id, "files": files}
        finally:
            db.close()

    try:
        result = await run_in_db(_attach)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, **result}


@router.post("/file")
async def create_and_attach_bootloader_file(body: dict, user: dict = Depends(require_user)):
    """Create a new bootloader file, then attach it to the requested layer."""
    def _create():
        db = get_db()
        try:
            target = _target_from_body(body, db, user["id"])
            requested_path = str(body.get("path") or "")
            if target.scope == "project":
                relative = Path(requested_path.strip())
                if (
                    not requested_path.strip()
                    or relative.is_absolute()
                    or len(relative.parts) != 1
                    or relative.suffix != ".md"
                ):
                    raise ValueError(
                        "Project context files must be direct context/*.md files"
                    )
            created = create_bootloader_file(
                target,
                requested_path,
                content=str(body.get("content") or ""),
            )
            current: list[str] | None = None
            if target.scope == "project":
                files = _current_files(target, db)
                from registry.bootload import (
                    report_sync_failure,
                    sync_bootload_documents,
                )

                failure = sync_bootload_documents(
                    target.target_id,
                    target.root.parent,
                    files,
                    db=db,
                )
                report_sync_failure(
                    target.target_id, failure, source="context_file_create"
                )
            else:
                current = _current_files(target, db)
                files = normalize_bootloader_entries(
                    [*current, created],
                    target,
                    existing_entries=set(current),
                )
            try:
                _write_context_change(
                    db,
                    target=target,
                    user_id=user["id"],
                    action="create",
                    files=files,
                    expected_files=current,
                )
            except ValueError as exc:
                raise ValueError(
                    f"File saved at {created}, but context was not updated: {exc}"
                ) from exc
            return {
                "scope": target.scope,
                "target_id": target.target_id,
                "path": str((target.root / created).resolve(strict=False))
                if not Path(created).is_absolute()
                else created,
                "stored_path": (
                    f"context/{created}" if target.scope == "project" else created
                ),
                "files": files,
            }
        finally:
            db.close()

    try:
        result = await run_in_db(_create)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, **result}
