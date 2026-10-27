"""Observe a connected-app result after its canonical stamped row was persisted."""
from __future__ import annotations

import json

from infra.database import get_db
from registry.observer import observe_command_result
from registry.registration_writer import RegistrationOrigin
from registry.service import notify_changed
from tools.mcp_stamp import APP_FIELD, TOOL_FIELD


def observe_persisted(thread_id: str, message_id: str, turn_id: str, args: dict) -> list[str]:
    db = get_db()
    changed: list[dict] = []
    told: list[str] = []
    try:
        row = db.execute("SELECT content, metadata FROM messages WHERE id=? AND thread_id=? AND role='tool'",
                         (message_id, thread_id)).fetchone()
        if row is None:
            return told
        metadata = json.loads(row["metadata"] or "{}")
        if not metadata.get(APP_FIELD) or not metadata.get(TOOL_FIELD):
            return told
        observe_command_result(thread_id, metadata.get("name"), args, row["content"] or "",
                               metadata=metadata, is_error=bool(metadata.get("is_error")),
                               db=db, told=told, changed=changed,
                               origin=RegistrationOrigin(thread_id, turn_id, "observe_rule"))
    finally:
        db.close()
    for artifact in changed:
        notify_changed(artifact)
    return told


def notice_for_result(thread_id: str, message_id: str, turn_id: str, args: dict) -> list[str]:
    """Keep an observer failure from withholding a completed tool result."""
    try:
        return observe_persisted(thread_id, message_id, turn_id, args)
    except Exception:
        from loguru import logger

        logger.exception("connected-app observation failed for {}", message_id)
        return ["Could not check whether to register this result. Register it manually if needed."]
