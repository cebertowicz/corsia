"""Canonical WebSocket projection for durable input rows."""

from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from typing import Any, Callable

from loguru import logger

from core.installed_context import INSTALLED_SOURCES
from core.viewing_context import VIEWING_DOCUMENT_KEY, VIEWING_DOCUMENT_TARGET_KEY
from infra.chat_comment_answers import message_answer_fields
from infra.chat_comment_index import comment_only
from infra.database import get_db
from infra.db_async import run_in_db
from infra.turn_errors import apply_error_next_steps, last_turn_error_id


_PRIVATE_METADATA_KEYS = (
    "runtime_context",
    "_corsia_send_delivery_state",
    "_corsia_queued_accepted_effects",
    "_corsia_queued_runtime_context",
    "_corsia_queued_delivery_options",
    "_corsia_vendor_login",
    "reply_via_post_update",
    # The caller's dict still holds what the client sent; only the stored row
    # holds the server-resolved note, and no client reads either.
    VIEWING_DOCUMENT_KEY,
    VIEWING_DOCUMENT_TARGET_KEY,
    INSTALLED_SOURCES,
)


@dataclass(frozen=True, slots=True)
class InputBroadcastRow:
    """One already-settled canonical input row ready for public projection."""

    message_id: str
    content: str
    metadata: dict | None
    created_at: str
    delivery_state: str = "delivered"
    delivery_error: str | None = None
    delivery_revision: int = 1
    seq: int | None = None
    role: str = "user"
    active_turn_id: str | None = None
    write_outcome: str | None = None
    delivery_intent: str | None = None
    answer_fields: dict | None = None


def read_input_broadcast_row(
    message_id: str,
    *,
    db: sqlite3.Connection | None = None,
) -> InputBroadcastRow | None:
    """Read one durable input through the canonical public broadcast shape.

    The role guard keeps an ordinary assistant turn from becoming an input
    broadcast; the one exception is a row explicitly marked ``for_user``
    (post_update's own agent-to-user update), which is exactly the class of
    assistant row the client already renders as user-facing.
    """
    connection = db if db is not None else get_db()
    owns_connection = db is None
    try:
        row = connection.execute(
            "SELECT id,thread_id,role,content,metadata,created_at,delivery_state,delivery_error,"
            "delivery_revision,seq,active_turn_id,write_outcome,delivery_intent FROM messages WHERE id=? "
            "AND (role IN ('user','system') "
            "OR (role='assistant' AND json_extract(metadata,'$.for_user')=1))",
            (message_id,),
        ).fetchone()
        if row is None:
            return None
        raw_metadata = row["metadata"]
        if raw_metadata is None:
            metadata = None
        else:
            try:
                metadata = json.loads(raw_metadata)
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
        apply_error_next_steps(
            connection, str(row["thread_id"]), [{"id": str(row["id"]), "created_at": row["created_at"], "metadata": metadata}],
        )
        return InputBroadcastRow(
            str(row["id"]),
            str(row["content"]),
            metadata,
            str(row["created_at"]),
            str(row["delivery_state"]),
            row["delivery_error"],
            int(row["delivery_revision"]),
            int(row["seq"]) if row["seq"] is not None else None,
            role=str(row["role"]),
            active_turn_id=row["active_turn_id"],
            write_outcome=row["write_outcome"],
            delivery_intent=row["delivery_intent"],
            # An update can answer chat comments mid-turn; the page must not wait for the turn's end to place them.
            answer_fields=message_answer_fields(
                str(row["thread_id"]), str(row["id"]), connection,
            ) if row["role"] == "assistant" else None,
        )
    finally:
        if owns_connection:
            connection.close()


def project_input_message(row: InputBroadcastRow) -> dict:
    """Serialize the durable row identically for singleton and batch notices."""
    metadata = dict(row.metadata) if isinstance(row.metadata, dict) else row.metadata
    if row.delivery_intent == "queue_after_turn":
        metadata = dict(metadata or {})
        metadata["delivery_intent"] = "queue_after_turn"
    elif row.delivery_intent is not None and isinstance(metadata, dict):
        metadata.pop("delivery_intent", None)
    if isinstance(metadata, dict):
        for key in _PRIVATE_METADATA_KEYS:
            metadata.pop(key, None)
    return {
        "id": row.message_id,
        "role": row.role,
        "content": row.content,
        "metadata": metadata,
        "created_at": row.created_at,
        "delivery_state": row.delivery_state,
        "delivery_error": row.delivery_error,
        "delivery_revision": row.delivery_revision,
        "seq": row.seq,
        "active_turn_id": row.active_turn_id,
        "write_outcome": row.write_outcome,
        **(row.answer_fields or {}),
    }


def broadcast_input_row(
    thread_id: str,
    message_id: str,
    content: str,
    metadata: dict | None,
    created_at: str,
    *,
    delivery_state: str = "delivered",
    delivery_error: str | None = None,
    write_outcome: str | None = None,
    delivery_revision: int = 1,
    seq: int | None = None,
    role: str = "user",
    exclude_ws: Any | None = None,
    broadcast: Callable[[str, dict], None] | None = None,
) -> None:
    """Publish one canonical input through the shared row projection."""
    broadcast_input_rows(
        thread_id,
        (InputBroadcastRow(
            message_id,
            content,
            metadata,
            created_at,
            delivery_state,
            delivery_error,
            delivery_revision,
            seq,
            role=role,
            write_outcome=write_outcome,
        ),),
        exclude_ws=exclude_ws,
        broadcast=broadcast,
    )


def input_rows_payload(thread_id: str, rows: tuple[InputBroadcastRow, ...]) -> dict:
    """The one frame shape for saved rows: a singleton or one ordered multi-row event."""
    messages = [project_input_message(row) for row in rows]
    return {
        "type": "message_added" if len(messages) == 1 else "messages_added",
        "thread_id": thread_id,
        **({"message": messages[0]} if len(messages) == 1 else {"messages": messages}),
    }


def broadcast_input_rows(
    thread_id: str,
    rows: tuple[InputBroadcastRow, ...],
    *,
    exclude_ws: Any | None = None,
    broadcast: Callable[[str, dict], None] | None = None,
) -> None:
    """Publish the drawn rows as one singleton or one ordered multi-row event.

    A comment-only user input is not a chat row: it lives in its comment thread,
    so chat clients get a `chat_comments_changed` notice naming the commented
    rows (by seq) instead of the row itself. A batch holding both goes out as
    two frames: the drawn rows' event, then the notice.
    """
    if broadcast is not None and exclude_ws is not None:
        raise ValueError("custom input broadcast cannot exclude one socket")
    hidden = [row.role == "user" and comment_only(row.content, row.metadata) for row in rows]
    drawn = tuple(row for row, comment in zip(rows, hidden) if not comment)
    comments = [row for row, comment in zip(rows, hidden) if comment]
    payloads = [input_rows_payload(thread_id, drawn)] if drawn else []
    if comments:
        payloads.append(chat_comments_changed_payload(thread_id, comments))
    import infra.state as state

    for payload in payloads:
        if broadcast is not None:
            broadcast(thread_id, payload)
        elif exclude_ws is None:
            state.broadcast_to_thread(thread_id, payload)
        else:
            state.broadcast_to_thread(thread_id, payload, exclude_ws=exclude_ws)


def chat_comments_changed_payload(thread_id: str, rows: list[InputBroadcastRow]) -> dict:
    """Tell chat clients which commented rows to re-read, without the input row."""
    seqs = sorted({
        comment["seq"]
        for row in rows
        for comment in (row.metadata or {})["reply_context"]["comments"]
        if isinstance(comment, dict) and isinstance(comment.get("seq"), int)
    })
    return {"type": "chat_comments_changed", "thread_id": thread_id, "target_seqs": seqs}


def _read_input_broadcast_rows(
    message_ids: tuple[str, ...],
) -> tuple[InputBroadcastRow, ...]:
    connection = get_db()
    try:
        return tuple(
            row
            for message_id in message_ids
            if (row := read_input_broadcast_row(message_id, db=connection)) is not None
        )
    finally:
        connection.close()


def broadcast_stored_message(
    thread_id: str,
    message_id: str,
    *,
    db: sqlite3.Connection | None,
    broadcast: Callable[[str, dict], None],
) -> None:
    """Project a runtime notice from its writer's connection, including its position."""
    row = read_input_broadcast_row(message_id, db=db)
    if row is None:
        raise ValueError(f"Cannot broadcast missing message {message_id}")
    broadcast_input_rows(thread_id, (row,), broadcast=broadcast)


async def broadcast_message_rows(
    thread_id: str,
    message_ids: tuple[str, ...],
    *,
    broadcast: Callable[[str, dict], None] | None = None,
) -> None:
    """Read settled rows off-loop, then publish them on the event loop."""
    rows = await run_in_db(_read_input_broadcast_rows, message_ids)
    if rows:
        broadcast_input_rows(thread_id, rows, broadcast=broadcast)


def _read_last_turn_error(thread_id: str) -> str | None:
    connection = get_db()
    try:
        return last_turn_error_id(connection, thread_id)
    finally:
        connection.close()


async def broadcast_newest_error(thread_id: str) -> None:
    """Re-project the error of the thread's latest, failed Turn after the queue changes.

    Its next step is projected from the queue, so a stored row the client
    already shows as 'failed' becomes 'held' only through this re-broadcast.
    A display projection never moves delivery: a failure is logged, not raised.
    """
    try:
        error_id = await run_in_db(_read_last_turn_error, thread_id)
        if error_id is not None:
            await broadcast_message_rows(thread_id, (error_id,))
    except Exception:
        logger.exception("newest-error re-broadcast failed for thread {}", thread_id)


async def broadcast_claimed_batch(batch) -> None:
    """Publish every row in one claimed FIFO batch through the canonical shape.

    Error rows this claim moved past were written before this batch's inputs,
    so they go first to keep the published rows in seq order.
    """
    await broadcast_message_rows(
        batch.thread_id,
        (*batch.error_message_ids, *(item.message_id for item in batch.items)),
    )


async def announce_successor_turn(batch) -> None:
    """Announce a successor Turn at its claim, not only once its harness accepts it.

    A Turn claimed with no sender showing it as working (a predecessor's
    terminal, a restart drain, a lifted hold, a spawn) would read as idle while
    its harness starts. Sent once, at the
    claim: the batch's later re-publishes (after a Stop or an error) must never
    announce a Turn that has already ended.
    """
    from core.turn_activity import turn_activity_frame
    import infra.state as state

    state.broadcast_to_thread(batch.thread_id, await turn_activity_frame(batch.thread_id, batch.identity))
