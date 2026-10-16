"""What the local screen concluded about each message: facts about mail, never its text.

There is deliberately no column that can hold an email's subject or body; the
only free-text field is ``kind``, which the screen's schema caps at 40
characters of lowercase letters, spaces and hyphens.
"""

from __future__ import annotations

EMAIL_SCREENS_SCHEMA_SQL = """
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
    );
"""
