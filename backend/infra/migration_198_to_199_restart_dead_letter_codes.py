"""Schema 198 -> 199: restart replay dead letters store their reason code.

Before 199, ``automation.restart_webhooks`` wrote the replay exception's text
as a ``dead_letter_events.reason``; it now writes the code
``restart_replay_poison`` and keeps the text on the webhook delivery row. This
step gives the rows written before the change the same code, so a read of set
aside events over any window names their real reason.

The step also adds ``code_named``: each writer now stores whether CORSIA's
code chose the event name, so an install-wide read never guesses it from
the name. Rows written before 199 keep the default 0 (counted as named by a
person or an agent); no rule tells them apart exactly, since a declared
type's stored suffix or a script's event may equal any code name.

Restart rows are found exactly: each restart dead letter's id is the uuid5 of
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


def migrate_198_to_199(db: sqlite3.Connection) -> None:
    """Add ``code_named``; rewrite each restart dead letter's stored text to its code."""
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dead_letter_events'"
    ).fetchone() is None:
        return
    columns = {row[1] for row in db.execute("PRAGMA table_info(dead_letter_events)")}
    if "code_named" not in columns:
        db.execute(
            "ALTER TABLE dead_letter_events ADD COLUMN code_named INTEGER NOT NULL DEFAULT 0"
        )
    ids = [
        (_CODE, row_id)
        for row_id, external_id in db.execute(_CANDIDATES_SQL, (_CODE,))
        if row_id == restart_dead_letter_id(external_id)
    ]
    db.executemany("UPDATE dead_letter_events SET reason=? WHERE id=?", ids)
