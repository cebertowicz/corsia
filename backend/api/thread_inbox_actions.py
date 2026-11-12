"""Explicit actions on canonical FIFO message rows."""

from __future__ import annotations

import json
import uuid

from fastapi import HTTPException

from api.thread_message_rows import public_delivery_message
from core.reply_context import DELIVERY_WARNING
from infra.chat_comment_index import comment_only
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


_ENDED_ATTEMPT_STATES = frozenset({"failed", "ambiguous", "cancelled"})


def _retry_id(message_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"corsia:retry:{message_id}"))


def _latest_attempt(thread_id: str, message: dict) -> dict:
    """Follow a row's Retry copies to the newest; one read per earlier Retry that sent."""
    db = get_db()
    try:
        while True:
            row = db.execute(
                "SELECT * FROM messages WHERE id=? AND thread_id=?",
                (_retry_id(str(message["id"])), thread_id),
            ).fetchone()
            if row is None:
                return message
            message = dict(row)
            if message["delivery_state"] not in _ENDED_ATTEMPT_STATES:
                return message
    finally:
        db.close()


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
        if comment_only(content, metadata):
            # A comment's copy has no text, so it stays out of chat; its reply context carries the warning.
            metadata["reply_context"] = {**metadata["reply_context"], "possible_duplicate": True}
        else:
            content = f"{DELIVERY_WARNING}\n\n{content}"
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
    # One copy per Retry that sends: a second Retry on the same row, from a
    # double press or another device, answers with its live copy instead of
    # sending again, and retries the copy if that one ended too. Admission's
    # duplicate check can't: a copy's metadata gains stamps once a Turn takes it in.
    message = await run_in_db(_latest_attempt, thread_id, message)
    if message["delivery_state"] not in _ENDED_ATTEMPT_STATES:
        return _revived(thread_id, str(message["id"]))
    retry_id = _retry_id(str(message["id"]))
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
            # A concurrent second press lands on admission's duplicate path.
            input_id=retry_id,
            native_mode="event",
            # The stored content was normalized when first accepted; the
            # retry resends that exact input.
            trim_content=False,
        )
    except SendRejected as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    except ValueError:
        # A concurrent press admitted this copy first and a Turn stamped it
        # before this press reached admission's duplicate check.
        latest = await run_in_db(_latest_attempt, thread_id, message)
        if str(latest["id"]) != retry_id:
            raise
        return _revived(thread_id, retry_id)
    receipt = getattr(result, "receipt", None)
    revived_id = getattr(result, "message_id", None) or getattr(
        receipt, "message_id", None,
    )
    return _revived(thread_id, revived_id)


def _revived(thread_id: str, message_id: str | None) -> dict:
    return {"thread_id": thread_id, "retried": True, "revived_message_id": message_id}
