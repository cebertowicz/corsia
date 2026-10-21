"""Persistence boundary for native Turn-error notices."""

from __future__ import annotations

from infra.turn_errors import apply_error_next_steps, insert_turn_error

# Row metadata the live error frame carries, when present.
ERROR_FRAME_KEYS = (
    "turn_id", "reference", "next_step", "cause_kind", "resets_at", "own_server",
)


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
    """Write one native Turn error and return its projected metadata.

    ``cause`` is the step payload's ``error_fields`` (``cause_kind``, and
    ``resets_at`` / ``own_server`` when known), stored on the row.
    """
    metadata = {"type": "llm_error", "model": model, **cause}
    with write_transaction_fn(None, scope="persist_error") as db:
        insert_turn_error(
            insert_message_fn, thread_id, content, metadata,
            turn_id=turn_id, msg_id=error_id, db=db, created_at=created_at,
        )
        apply_error_next_steps(db, thread_id, [{"id": error_id, "metadata": metadata}])
    return metadata
