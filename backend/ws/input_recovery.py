"""Answer an input a reconnecting client resends, without admitting it twice."""

from __future__ import annotations

from fastapi import WebSocket

from infra import state
from infra.db_async import run_read


def _stored_input(thread_id: str, input_id: str) -> dict | None:
    from infra.database import get_db

    connection = get_db()
    try:
        row = connection.execute(
            "SELECT id,created_at,delivery_state,delivery_error,delivery_revision "
            "FROM messages WHERE id=? AND thread_id=?",
            (input_id, thread_id),
        ).fetchone()
    finally:
        connection.close()
    return dict(row) if row is not None else None


async def answer_recovered_input(
    ws: WebSocket, thread_id: str, input_id: str,
) -> bool:
    """Tell the client what became of a resent input this thread already stores.

    After a reconnect the client resends, in the order it first sent them,
    every input it never got a receipt for, marked ``recovery``. One the server
    already stores is answered with its stored row and is not admitted again:
    delivery can write to a stored row, so the duplicate check would refuse the
    resent frame as a different payload. False means nothing is stored under
    that id, and the caller admits the frame as the new input it is.
    """
    row = await run_read(_stored_input, thread_id, input_id)
    if row is None:
        return False
    try:
        await state.send_to_client(ws, {
            "type": "input_recovered",
            "thread_id": thread_id,
            "input_id": input_id,
            "message": row,
        })
    except (RuntimeError, ConnectionError, AttributeError):
        pass
    return True
