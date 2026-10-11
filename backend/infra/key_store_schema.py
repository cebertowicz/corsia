"""Key store DDL: entry metadata and the per-session wrong-password count.

Neither table ever holds a key value. Values live in their own file in
CORSIA's private state (``infra.key_store``), so nothing that reads, backs up
or exports the database can carry one.

``key_store_entries`` holds what the Settings › Keys row shows: an added
entry's name, label, description and who added it, and, for every entry, when
its value was last set there. ``value_ref`` names the entry's live value in the
values file; NULL means none is set. A built-in entry (web search) gets a row only to
carry that date; its label and use are fixed in code.

``key_write_attempts`` counts wrong passwords on key writes per session, keyed
by the session token's digest, so a restart does not reset it. It is separate
from sign-in's throttle on purpose: wrong passwords here must never lock the
owner out of sign-in.
"""

from __future__ import annotations

KEY_STORE_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS key_store_entries (
        name TEXT PRIMARY KEY,
        label TEXT,
        description TEXT,
        added_by_thread_id TEXT,
        added_by_name TEXT,
        created_at TEXT NOT NULL,
        set_at TEXT,
        value_ref TEXT
    );

    CREATE TABLE IF NOT EXISTS key_write_attempts (
        token_hash TEXT PRIMARY KEY
            REFERENCES auth_tokens(token_hash) ON DELETE CASCADE,
        failures INTEGER NOT NULL DEFAULT 0 CHECK (failures >= 0),
        updated_at TEXT NOT NULL
    );
"""
