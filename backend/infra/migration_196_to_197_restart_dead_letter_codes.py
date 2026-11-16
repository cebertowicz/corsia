"""Schema 196 -> 197: restart replay dead letters store their reason code.

Before 197, ``automation.restart_webhooks`` wrote the replay exception's text
as a ``dead_letter_events.reason``; it now writes the code
``restart_replay_poison`` and keeps the text on the webhook delivery row. This
step gives the rows written before the change the same code, so a read of set
aside events over any window names their real reason.

Those rows are found exactly: each restart dead letter's id is the uuid5 of
its delivery id (its ``external_id``) under a fixed seed, which no other
writer produces. Rows already holding the code are left as they are. The step
runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3
import uuid

#: Frozen with this step: ``restart_webhooks._DEAD_LETTER_SEED`` and ``DEAD_LETTER_REASON``.
_SEED = "corsia-webhook-dead-letter"
_CODE = "restart_replay_poison"

_CANDIDATES_SQL = (
    "SELECT id, external_id FROM dead_letter_events "
    "WHERE internal_event LIKE 'webhook.github.%' AND external_id IS NOT NULL AND reason != ?"
)


def restart_dead_letter_id(delivery_id: str) -> str:
    """The id ``restart_webhooks`` gives the dead letter of one delivery."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{_SEED}:{delivery_id}"))


def migrate_196_to_197(db: sqlite3.Connection) -> None:
    """Rewrite each restart dead letter's stored exception text to its code."""
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dead_letter_events'"
    ).fetchone() is None:
        return
    ids = [
        (_CODE, row_id)
        for row_id, external_id in db.execute(_CANDIDATES_SQL, (_CODE,))
        if row_id == restart_dead_letter_id(external_id)
    ]
    db.executemany("UPDATE dead_letter_events SET reason=? WHERE id=?", ids)
