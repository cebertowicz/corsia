"""Read a delegated thread's status and permitted recent output."""
from __future__ import annotations

from typing import Any

from infra.delegation_status import held_admission_status
from infra.queries import get_thread_for_user


def _recent_message(row: Any) -> dict:
    from core.message_display import durable_row_text

    message = dict(row)
    message["content"] = durable_row_text(message["content"], message.pop("metadata"))
    return message


def _checkpoint_status(checkpoint) -> dict[str, Any]:
    return {
        "id": checkpoint.id, "trigger": checkpoint.trigger, "mode": checkpoint.mode,
        "status": checkpoint.status, "writer_model": checkpoint.writer_model,
        "error": checkpoint.error, "started_at": checkpoint.started_at,
    }


def read_delegate_status(thread_id: str, user_id: str, message_count: int, caller_id: str):
    from infra.conductor import get_coordination_scopes
    from infra.checkpoint_queries import latest_for_thread
    from infra.database import get_db
    from infra.thread_confinement import (
        ISOLATED_RESULT_LABEL, finished_agent_result, restrict_agent_read,
    )

    db = get_db()
    try:
        thread_row = get_thread_for_user(thread_id, user_id, db=db)
        if not thread_row:
            return None
        active = db.execute(
            "SELECT id, status, started_at FROM active_turns "
            "WHERE thread_id = ? AND status='running' "
            "ORDER BY started_at DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        restricted = restrict_agent_read(thread_row["confinement"], thread_id, caller_id)
        recent = db.execute(
            "SELECT id, role, content, metadata, created_at FROM messages "
            "WHERE thread_id = ? AND delivery_state = 'delivered' "
            "ORDER BY created_at DESC LIMIT ?",
            (thread_id, message_count),
        ).fetchall() if message_count and not restricted else []
        final = finished_agent_result(db, thread_id) if message_count and restricted else None
        if final is not None:
            final["content"] = f"{ISOLATED_RESULT_LABEL}\n\n{final['content']}"
        checkpoint = latest_for_thread(thread_id, db=db)
        thread = dict(thread_row)
        if restricted:
            thread["title"] = None
        thread["held_admission"] = held_admission_status(db, thread, dict(active) if active else None)
        return (
            thread,
            dict(active) if active else None,
            get_coordination_scopes(thread_id, db=db),
            [final] if final else [_recent_message(row) for row in reversed(recent)],
            None if checkpoint is None else (
                {"id": checkpoint.id, "status": checkpoint.status, "started_at": checkpoint.started_at}
                if restricted else _checkpoint_status(checkpoint)
            ),
        )
    finally:
        db.close()
