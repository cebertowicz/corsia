"""Register project context files in the artifact registry."""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from infra.database import get_db, write_transaction
from infra.product_events import emit_product_event
from registry import models, service
from registry.file_resolution import prepare_file_identity

def thread_sentinel(project_id: str) -> str:
    """The ``parent_thread_id`` sentinel for settings-driven rows."""
    return models.project_sentinel(project_id)


def sync_bootload_documents(
    project_id: str,
    workspace_dir: Path,
    files: list[str],
    db=None,
) -> Exception | None:
    """Register a project's context files as registry documents it owns.

    Returns the failure instead of raising so a caller can keep going; the
    caller passes it to ``report_sync_failure``. Agents read the files
    directly, so a failure only leaves them out of the project's document list.
    """
    _db = db or get_db()
    try:
        prepared = []
        for entry in files:
            path = (workspace_dir / entry).resolve()
            identity = prepare_file_identity(str(path), db=_db)
            if identity is None:
                raise ValueError(f"path is outside the registrable roots: {path}")
            prepared.append((entry, identity))
        with write_transaction(_db, scope="sync_bootload_documents") as connection:
            for entry, identity in prepared:
                service.register_write_strict(
                    thread_sentinel(project_id),
                    identity,
                    f"Bootloader context for this project ({entry}).",
                    title=Path(entry).name,
                    db=connection,
                )
        return None
    except Exception as e:
        return e
    finally:
        if not db:
            _db.close()


def report_sync_failure(project_id: str, error: Exception | None, *, source: str) -> None:
    """Log and count a registration failure; the caller carries on. No-op on success."""
    if error is None:
        return
    logger.warning(
        f"bootload_registry_sync_failed project_id={project_id} source={source}: {error}"
    )
    emit_product_event(
        "bootload",
        "registry_sync_failed",
        properties={
            "project_id": project_id,
            "source": source,
            "error": type(error).__name__,
        },
    )


def resolve_designation(project_id: str, db=None) -> list[str] | None:
    """Read the raw designation list off the project row (JSON or None)."""
    _db = db or get_db()
    try:
        r = _db.execute(
            "SELECT bootloader_files FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if r and r["bootloader_files"]:
            parsed = json.loads(r["bootloader_files"])
            return [str(x) for x in parsed] if isinstance(parsed, list) else None
        return None
    finally:
        if not db:
            _db.close()
