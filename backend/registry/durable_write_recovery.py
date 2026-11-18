"""Rebuild the policy decision a durable write intent persisted, for startup recovery."""

from __future__ import annotations

import logging
from pathlib import Path
import sqlite3

from core.workspace_layout import resolve_project_root
from registry import service
from registry.shared_principals import artifact_owner
from registry.shared_target import SharedTarget


logger = logging.getLogger(__name__)


def recover_intents(db, workspace: Path, recover_one) -> None:
    """Isolate document recovery failures while propagating database failures."""
    rows = db.execute(
        "SELECT * FROM durable_write_intents ORDER BY created_at,id"
    ).fetchall()
    for raw in rows:
        try:
            recover_one(dict(raw), workspace=workspace, db=db)
        except Exception as exc:
            if db.in_transaction:
                db.rollback()
            if isinstance(exc, sqlite3.OperationalError):
                raise
            logger.exception(
                "Could not recover durable write intent %s for %s.",
                raw["id"], raw["target_path"],
            )


def recovery_shared_target(target: Path, row: dict, *, workspace: Path, db) -> SharedTarget:
    """Rebuild the policy decision already persisted in an interrupted intent."""
    actor = str(row["actor_thread_id"])
    scope_id = "workspace"
    for project in db.execute("SELECT id FROM projects").fetchall():
        project_root = resolve_project_root(workspace, project["id"], db=db)
        if project_root is not None and target.is_relative_to(
            project_root.resolve(strict=False)
        ):
            scope_id = str(project["id"])
            break
    category_root = target.parent
    artifact = service.find_file_identity(str(target), db=db)
    owner = artifact_owner(str(artifact["id"]), db=db) if artifact is not None else None
    return SharedTarget(
        target,
        category_root,
        category_root / "INDEX.md",
        bool(row["created_parent"]),
        scope_id,
        actor_principal=str(owner or actor),
        owner_thread_id=str(owner) if owner else None,
    )
