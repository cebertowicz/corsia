"""Persistence boundary for native Turn-error notices."""

from __future__ import annotations

from infra.turn_errors import insert_turn_error


async def broadcast_llm_error(thread_id: str, error_id: str, broadcast) -> None:
    """Publish the saved error at its durable transcript position."""
    from infra.db_async import run_in_db
    from infra.send_broadcast import project_input_message, read_input_broadcast_row

    row = await run_in_db(read_input_broadcast_row, error_id)
    if row is None:
        raise RuntimeError(f"Cannot broadcast missing error {error_id}")
    await broadcast({"type": "message_added", "thread_id": thread_id,
                     "message": project_input_message(row)})


def persist_llm_error(
    write_transaction_fn,
    insert_message_fn,
    thread_id: str,
    model: object,
    turn_id: str,
    error_id: str,
    created_at: str,
    content: str,
    cause: dict,
) -> dict:
    """Write one native Turn error and return the fields its live frame carries.

    The Turn that wrote it is still running, so the frame carries no ``next_step``.

    ``cause`` is the step payload's ``error_fields`` (``cause_kind``, and
    ``resets_at`` / ``own_server`` when known), stored on the row and carried
    whole, so a field added there reaches the frame as it reaches history.
    """
    metadata = {"type": "llm_error", "model": model, **cause}
    with write_transaction_fn(None, scope="persist_error") as db:
        insert_turn_error(
            insert_message_fn, thread_id, content, metadata,
            turn_id=turn_id, msg_id=error_id, db=db, created_at=created_at,
        )
    # The Turn is still running, so no next_step yet: settlement re-publishes the row with its real value.
    return {**cause, **{key: metadata[key] for key in ("turn_id", "reference")}}
