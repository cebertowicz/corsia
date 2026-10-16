"""Schema 120 -> 121: the local screen's per-message results.

Purely additive: one new table, no data to convert. The DDL is copied rather
than imported so this step keeps producing the shape it was written for.
"""

from __future__ import annotations

import sqlite3

_EMAIL_SCREENS_120_DDL = """
    CREATE TABLE IF NOT EXISTS email_screens (
        account        TEXT NOT NULL,
        message_id     TEXT NOT NULL,
        status         TEXT NOT NULL
                       CHECK (status IN ('screened', 'pending', 'unavailable', 'failed')),
        kind           TEXT,
        needs_person   TEXT CHECK (needs_person IN ('now', 'today', 'no')),
        injection      INTEGER CHECK (injection IN (0, 1)),
        injection_rule TEXT,
        model_id       TEXT,
        screened_at    TEXT NOT NULL,
        attempts       INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (account, message_id)
    )
"""


def migrate_120_to_121(conn: sqlite3.Connection) -> None:
    """Create the email screens table."""
    conn.execute(_EMAIL_SCREENS_120_DDL)
