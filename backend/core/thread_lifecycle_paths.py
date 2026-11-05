"""Resolve the workspace location of a lifecycle-managed thread."""

from pathlib import Path


def thread_dir(db, sid: str, project_id: str | None, workspace: Path) -> Path | None:
    """Resolve ``.threads/{id}`` through the public Workspace layout owner."""
    from core.workspace_layout import (
        resolve_project_root,
        resolve_thread_root,
        resolve_workspace_root,
    )

    scope = (
        resolve_project_root(workspace, project_id, db=db)
        if project_id
        else resolve_workspace_root(workspace)
    )
    return resolve_thread_root(scope, sid) if scope is not None else None
