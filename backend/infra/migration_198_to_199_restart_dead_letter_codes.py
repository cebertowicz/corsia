"""Schema 198 -> 199: restart replay dead letters store their reason code.

Before 199, ``automation.restart_webhooks`` wrote the replay exception's text
as a ``dead_letter_events.reason``; it now writes the code
``restart_replay_poison`` and keeps the text on the webhook delivery row. This
step gives the rows written before the change the same code, so a read of set
aside events over any window names their real reason.

The step also adds ``code_named``. It marks old rows matching the platform
writer's envelope for connectors registered from declared-event intake's
introduction through this migration, excluding restart replay rows. Other old
rows remain private: their name and payload do not establish who chose the name.

Restart rows are found exactly: each restart dead letter's id is the uuid5 of
its delivery id (its ``external_id``) under a fixed seed, which no other
writer produces. Rows already holding the code are left as they are. The step
runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

#: Frozen with this step: ``restart_webhooks._DEAD_LETTER_SEED`` and ``DEAD_LETTER_REASON``.
_SEED = "corsia-webhook-dead-letter"
_CODE = "restart_replay_poison"
# Built-ins loaded when declared-event intake was introduced (and through 199).
# Frozen here: later registrations could previously have been declared namespaces.
_EARLY_CONNECTORS = frozenset({"document", "github", "gmail", "library", "measurement", "microsoft", "plan"})

_CANDIDATES_SQL = (
    "SELECT id, external_id FROM dead_letter_events "
    "WHERE internal_event LIKE 'webhook.github.%' AND external_id IS NOT NULL AND reason != ?"
)


def restart_dead_letter_id(delivery_id: str) -> str:
    """The id ``restart_webhooks`` gives the dead letter of one delivery."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{_SEED}:{delivery_id}"))


def _old_platform_code_named(row_id: str, external_id: str | None, payload: str | None) -> bool:
    # Restart replay stores arbitrary webhook payloads; even one shaped like an
    # envelope is not evidence of a platform writer.
    if external_id is None or row_id == restart_dead_letter_id(external_id):
        return False
    try:
        data = json.loads(payload or "")
    except (TypeError, ValueError):
        return False
    return (isinstance(data, dict) and set(data) == {"event_id", "connector", "facts"}
            and data["event_id"] == external_id and isinstance(data["facts"], dict)
            and isinstance(data["connector"], str) and data["connector"] in _EARLY_CONNECTORS)


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
        named = [(row_id,) for row_id, external_id, payload in db.execute(
            "SELECT id, external_id, payload_json FROM dead_letter_events"
        ) if _old_platform_code_named(row_id, external_id, payload)]
        db.executemany("UPDATE dead_letter_events SET code_named=1 WHERE id=?", named)
    ids = [
        (_CODE, row_id)
        for row_id, external_id in db.execute(_CANDIDATES_SQL, (_CODE,))
        if row_id == restart_dead_letter_id(external_id)
    ]
    db.executemany("UPDATE dead_letter_events SET reason=? WHERE id=?", ids)
