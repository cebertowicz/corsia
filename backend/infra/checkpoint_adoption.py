"""The size an installed checkpoint's continuation measured when a Turn first adopted it."""

from __future__ import annotations

import sqlite3

from infra.database import write_transaction


def record_adopted_input_tokens(
    checkpoint_id: str,
    tokens: int,
    *,
    db: sqlite3.Connection | None = None,
) -> None:
    """Write the continuation's size at first adoption; the first writer wins."""
    with write_transaction(db, scope="checkpoint_adopted_tokens") as connection:
        connection.execute(
            "UPDATE checkpoints SET adopted_input_tokens=? "
            "WHERE id=? AND status='installed' AND adopted_input_tokens IS NULL",
            (tokens, checkpoint_id),
        )
