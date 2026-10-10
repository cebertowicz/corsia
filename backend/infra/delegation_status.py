"""Read-only projections shared by delegated-thread status surfaces."""

from __future__ import annotations

from typing import Any

from infra.held_admission import limited_admission


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
