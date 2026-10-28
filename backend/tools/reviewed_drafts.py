"""Provider-neutral storage for person-reviewed outbound draft documents."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from core.workspace_layout import resolve_project_root, resolve_thread_working_root, resolve_workspace_root
from infra import config
from infra.database import get_db
from infra.db_async import run_in_db
from tools.action_drafts import DraftError, create_draft, record_draft
from tools.action_gate import _read_bytes
from tools.filesystem import WriteFileTool


PROJECT_UNAVAILABLE = "The project's folder is unavailable, so the draft was not saved."
OUTSIDE_WORKSPACE = "The draft's folder is outside the workspace, so it was not saved."
NO_OPEN_DRAFT = "There is no open draft with that id in this conversation."
OTHER_ACCOUNT = (
    "This draft was written from {address}. To use another account, save a new draft "
    "(omit draft_id) with that account_id and show the person."
)
CHANGED_WHILE_SAVING = "The draft changed while it was being saved, so it is not recorded. Save it again."


class DraftStorageRefused(ValueError):
    """A safe refusal from reviewed-draft storage."""


def _workspace_root() -> Path:
    return resolve_workspace_root(config.WORKSPACE)


def draft_target(row: dict[str, Any] | None) -> dict[str, Any]:
    try:
        value = json.loads((row or {}).get("target_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def working_root(thread_id: str) -> Path:
    workspace = _workspace_root()
    db = get_db()
    try:
        row = db.execute("SELECT project_id FROM threads WHERE id=?", (thread_id,)).fetchone()
        project_id = row["project_id"] if row else None
        scope = resolve_project_root(workspace, project_id, db=db) if project_id else workspace
    finally:
        db.close()
    if scope is None:
        raise DraftStorageRefused(PROJECT_UNAVAILABLE)
    try:
        return resolve_thread_working_root(scope, thread_id)
    except ValueError as exc:
        raise DraftStorageRefused(PROJECT_UNAVAILABLE) from exc


def _relative(path: Path) -> str:
    relative = os.path.relpath(path.resolve(), _workspace_root())
    if relative == ".." or relative.startswith(f"..{os.sep}"):
        raise DraftStorageRefused(OUTSIDE_WORKSPACE)
    return relative


def _draft_path(
    thread_id: str, existing: dict[str, Any] | None, prefix: str, kind: str, extension: str,
) -> tuple[Path, str]:
    """Resolve (blocking: database and filesystem) where a draft is written."""
    if existing:
        path = _workspace_root() / str(existing["path"])
    else:
        path = working_root(thread_id) / prefix / f"{kind}-{uuid.uuid4().hex[:12]}{extension}"
    return path, _relative(path)


def open_draft(thread_id: str, draft_id: str, kind: str, *, db: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    conn = db if db is not None else get_db()
    try:
        row = conn.execute(
            "SELECT id,path,state,target_json FROM action_drafts WHERE id=? AND thread_id=? AND kind=?",
            (draft_id, thread_id, kind),
        ).fetchone()
        return dict(row) if row else None
    finally:
        if db is None:
            conn.close()


def checked_open_draft(thread_id: str, draft_id: object, kind: str, account_id: str) -> dict[str, Any] | None:
    if draft_id is None:
        return None
    row = open_draft(thread_id, str(draft_id), kind)
    if row is None or row["state"] != "open":
        raise DraftStorageRefused(NO_OPEN_DRAFT)
    bound = draft_target(row).get("account_id")
    if bound not in (None, account_id):
        raise DraftStorageRefused(OTHER_ACCOUNT.format(address=draft_target(row).get("account") or "another account"))
    return row


async def save_draft(
    *, thread_id: str, kind: str, extension: str, prefix: str, account_id: str,
    target: dict[str, Any], text: str, draft_id: object, context: dict[str, Any], description: str,
) -> dict[str, Any]:
    """Write and record a reviewed draft under the canonical thread working root."""
    existing = await run_in_db(checked_open_draft, thread_id, draft_id, kind, account_id)
    path, relative = await run_in_db(_draft_path, thread_id, existing, prefix, kind, extension)
    written = await WriteFileTool(workspace=config.WORKSPACE, allowed_dir=_workspace_root()).execute(
        path=str(path), content=text, description=description,
        **{key: value for key, value in context.items() if key.startswith("_")},
    )
    if isinstance(written, str) and written.startswith("Error"):
        raise DraftStorageRefused(written.removeprefix("Error: "))
    data = await run_in_db(_read_bytes, relative)
    if data != text.encode("utf-8"):
        raise DraftStorageRefused(CHANGED_WHILE_SAVING)
    if existing:
        return await run_in_db(record_draft, str(existing["id"]), data, target)
    return await run_in_db(create_draft, thread_id, kind, relative, data, target)
