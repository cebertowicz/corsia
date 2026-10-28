"""Read-only projections shared by delegated-thread status surfaces."""

from __future__ import annotations

from typing import Any

from infra.held_admission import limited_admission
from infra.thread_confinement import (
    Reader, agent_result, checkpoint_for_reader, label_result, may_read_transcript, project_thread,
)


def read_delegate_status(
    thread_id: str, user_id: str, message_count: int, reader: Reader, *, person_messages: bool = False,
):
    """One status snapshot for tools and APIs, with the same transcript boundary."""
    from infra.checkpoint_queries import latest_for_thread
    from infra.conductor import get_coordination_scopes
    from infra.database import get_db
    from infra.queries import get_thread_for_user

    db = get_db()
    try:
        thread_row = get_thread_for_user(thread_id, user_id, db=db)
        if not thread_row:
            return None
        active = db.execute(
            "SELECT id, status, started_at FROM active_turns WHERE thread_id=? AND status='running' "
            "ORDER BY started_at DESC LIMIT 1", (thread_id,),
        ).fetchone()
        restricted = not may_read_transcript(reader, thread_row)
        if restricted:
            final = agent_result(db, thread_id) if message_count else None
            recent = [] if final is None else [{**final, "content": label_result(final["content"])}]
        else:
            rows = db.execute(
                "SELECT id, role, content, metadata, created_at FROM messages WHERE thread_id=? "
                + ("" if person_messages else "AND delivery_state='delivered' ")
                + "ORDER BY created_at DESC LIMIT ?", (thread_id, message_count),
            ).fetchall() if message_count else []
            recent = [_status_message(row, person_messages) for row in reversed(rows)]
        checkpoint = latest_for_thread(thread_id, db=db)
        thread = project_thread(reader, thread_row)
        thread["held_admission"] = held_admission_status(db, thread, dict(active) if active else None)
        return (
            thread, dict(active) if active else None,
            get_coordination_scopes(thread_id, db=db), recent,
            checkpoint_for_reader(reader, thread_row, checkpoint),
        )
    finally:
        db.close()


def _status_message(row, person_messages: bool) -> dict:
    from core.message_display import durable_row_text

    message = dict(row)
    metadata = message.pop("metadata")
    if not person_messages:
        message["content"] = durable_row_text(message["content"], metadata)
    return message



def _clean(value: Any) -> str | None:
    text = (value or "").strip() if isinstance(value, str) else ""
    return text or None


def held_admission_status(db, thread_row: dict[str, Any], active: dict[str, Any] | None):
    """Project a current limited idle prefix without adding durable hold state."""
    if active is not None:
        return None
    thread_id = str(thread_row["id"])
    model = str(thread_row.get("model") or "").strip()
    if not model:
        return None
    queued = db.execute(
        "SELECT 1 FROM messages WHERE thread_id=? AND delivery_state='queued' "
        "AND active_turn_id IS NULL AND input_order IS NOT NULL "
        "AND target_model=? LIMIT 1",
        (thread_id, model),
    ).fetchone()
    return None if queued is None else limited_admission(thread_id, model, db=db)


def model_switch_status(thread_row: dict[str, Any]) -> dict[str, Any] | None:
    """Project the current model-switch operation for a sender."""
    operation_id = _clean(thread_row.get("switch_operation_id"))
    if operation_id is None:
        return None
    status = _clean(thread_row.get("switch_status"))
    return {
        "operation_id": operation_id, "pending_model": _clean(thread_row.get("pending_model")),
        "status": status, "active": status in {"preparing", "ready"},
        "checkpoint_model": _clean(thread_row.get("switch_checkpoint_choice")),
        "error": _clean(thread_row.get("switch_error")),
        "requested_at": _clean(thread_row.get("switch_requested_at")),
    }
