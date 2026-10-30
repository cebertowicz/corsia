"""Explicit actions on canonical FIFO message rows."""

from __future__ import annotations

import json
import uuid

from fastapi import HTTPException

from api.thread_message_rows import public_delivery_message
from infra.database import get_db
from infra.db_async import run_in_db
from infra.queries import get_thread_for_user
from infra.queued_input_settlement import cancel_queued_input
from infra.vendor_login import public_metadata


def _owned_input(thread_id: str, message_id: str, user_id: str):
    db = get_db()
    try:
        if get_thread_for_user(thread_id, user_id, db=db) is None:
            return None, None
        row = db.execute(
            "SELECT * FROM messages WHERE id=? AND thread_id=?",
            (message_id, thread_id),
        ).fetchone()
        thread_row = db.execute(
            "SELECT * FROM threads WHERE id=?", (thread_id,),
        ).fetchone()
        return (dict(thread_row), dict(row)) if row is not None else (dict(thread_row), None)
    finally:
        db.close()


def _cancel_input(
    thread_id: str,
    message_id: str,
    revision: int,
) -> dict:
    row = cancel_queued_input(
        thread_id, message_id, revision=revision,
    )
    return public_delivery_message(row)


def _broadcast(projected: dict) -> None:
    from infra.send_broadcast import InputBroadcastRow, broadcast_input_rows

    broadcast_input_rows(
        str(projected["thread_id"]),
        (InputBroadcastRow(
            str(projected["id"]),
            str(projected["content"]),
            projected.get("metadata"),
            str(projected["created_at"]),
            str(projected["delivery_state"]),
            projected.get("delivery_error"),
            int(projected["delivery_revision"]),
            projected.get("seq"),
            role=str(projected["role"]),
            active_turn_id=projected.get("active_turn_id"),
            write_outcome=projected.get("write_outcome"),
        ),),
    )


async def run_inbox_message_action(
    thread_id: str,
    message_id: str,
    user_id: str,
    *,
    action: str,
) -> dict:
    """Cancel one unclaimed input, then synchronously drive its next head."""
    if action != "cancel":
        raise HTTPException(422, f"unsupported message action: {action}")
    _thread_row, message = await run_in_db(
        _owned_input, thread_id, message_id, user_id,
    )
    if message is None:
        raise HTTPException(404, "Task or message not found")
    try:
        projected = await run_in_db(
            _cancel_input,
            thread_id,
            message_id,
            int(message["delivery_revision"]),
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    projected["thread_id"] = thread_id
    _broadcast(projected)
    from infra.send_broadcast import broadcast_newest_error
    from infra.thread_execution import drive_thread

    # A held error stops being held once nothing waits behind it.
    await broadcast_newest_error(thread_id)

    await drive_thread(thread_id)
    return {"thread_id": thread_id, "message": projected}


def _retry_content(message: dict) -> tuple[str, dict]:
    try:
        metadata = json.loads(message.get("metadata") or "{}")
    except (TypeError, json.JSONDecodeError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    metadata = public_metadata(metadata)
    metadata.pop("turn_id", None)
    metadata.pop("input_id", None)
    metadata = {
        **metadata,
        "retried_from_message_id": str(message["id"]),
    }
    content = str(message.get("content") or "")
    if message.get("delivery_state") == "ambiguous":
        metadata["possible_duplicate"] = True
        content = (
            "[DELIVERY WARNING: the prior attempt may have reached the model; "
            "this retry may duplicate it.]\n\n" + content
        )
    return content, metadata


async def retry_thread_delivery_now(
    thread_id: str,
    message_id: str,
    user_id: str,
) -> dict:
    """Drive pending work, or create a new warned input for an ended attempt."""
    thread_row, message = await run_in_db(
        _owned_input, thread_id, message_id, user_id,
    )
    if thread_row is None or message is None:
        raise HTTPException(404, "Task or message not found")
    state = str(message["delivery_state"])
    if state == "queued":
        from infra.thread_execution import drive_thread

        await drive_thread(thread_id)
        return {"thread_id": thread_id, "retried": True, "message": message}
    if state not in {"failed", "ambiguous"}:
        raise HTTPException(409, "This message has no retryable delivery outcome.")
    content, metadata = _retry_content(message)
    # send_thread_input owns the empty-input rule.
    from infra.send_service import SendRejected, send_thread_input

    try:
        result = await send_thread_input(
            thread_id,
            thread_row,
            content,
            user={"id": user_id},
            role=str(message["role"]),
            metadata=metadata,
            input_id=str(uuid.uuid4()),
            native_mode="event",
            # The stored content was normalized when first accepted; the
            # retry resends that exact input.
            trim_content=False,
        )
    except SendRejected as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    receipt = getattr(result, "receipt", None)
    revived_id = getattr(result, "message_id", None) or getattr(
        receipt, "message_id", None,
    )
    return {
        "thread_id": thread_id,
        "retried": True,
        "revived_message_id": revived_id,
    }
