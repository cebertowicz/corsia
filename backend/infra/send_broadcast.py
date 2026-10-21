"""Canonical WebSocket projection for durable input rows."""

from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from typing import Any, Callable

from core.installed_context import INSTALLED_SOURCES
from core.reply_context import REPLY_CONTEXT_KEY
from core.viewing_context import VIEWING_DOCUMENT_KEY, VIEWING_DOCUMENT_TARGET_KEY
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
    # The sender's own context for the agent; no client reads it back.
    REPLY_CONTEXT_KEY,
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
            connection, str(row["thread_id"]), [{"id": str(row["id"]), "metadata": metadata}],
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


def broadcast_input_rows(
    thread_id: str,
    rows: tuple[InputBroadcastRow, ...],
    *,
    exclude_ws: Any | None = None,
    broadcast: Callable[[str, dict], None] | None = None,
) -> None:
    """Publish one singleton or one atomically ordered multi-row event."""
    if not rows:
        return
    messages = [project_input_message(row) for row in rows]
    payload = {
        "type": "message_added" if len(messages) == 1 else "messages_added",
        "thread_id": thread_id,
        **({"message": messages[0]} if len(messages) == 1 else {"messages": messages}),
    }
    if broadcast is not None:
        if exclude_ws is not None:
            raise ValueError("custom input broadcast cannot exclude one socket")
        broadcast(thread_id, payload)
        return
    import infra.state as state

    if exclude_ws is None:
        state.broadcast_to_thread(thread_id, payload)
    else:
        state.broadcast_to_thread(thread_id, payload, exclude_ws=exclude_ws)


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
    """
    error_id = await run_in_db(_read_last_turn_error, thread_id)
    if error_id is not None:
        await broadcast_message_rows(thread_id, (error_id,))


async def broadcast_claimed_batch(batch) -> None:
    """Publish every row in one claimed FIFO batch through the canonical shape.

    Recovered rows were stamped before this batch's inputs, so they go first
    to keep the published rows in seq order.
    """
    await broadcast_message_rows(
        batch.thread_id,
        (*batch.recovered_message_ids, *(item.message_id for item in batch.items)),
    )
