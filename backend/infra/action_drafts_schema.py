"""Drafts of outbound actions (email, calendar invites) awaiting the person's reply.

``recorded_revision`` and ``recorded_at`` are written only by
``tools.action_drafts``; the action gate compares the document's current bytes
and the person's newest message against them, never against a file timestamp.
"""

from __future__ import annotations

ACTION_DRAFTS_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS action_drafts (
        id                TEXT PRIMARY KEY,
        thread_id         TEXT NOT NULL REFERENCES threads(id),
        kind              TEXT NOT NULL CHECK (kind IN ('email', 'invite')),
        path              TEXT NOT NULL,
        target_json       TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(target_json)),
        state             TEXT NOT NULL DEFAULT 'open'
                          CHECK (state IN ('open', 'sending', 'sent', 'discarded')),
        recorded_revision TEXT NOT NULL,
        recorded_at       TEXT NOT NULL,
        created_at        TEXT NOT NULL,
        sent_at           TEXT,
        external_id       TEXT
    );
"""
