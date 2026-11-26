"""Key store DDL: entry metadata, saved website logins and the per-session wrong-password count.

No table ever holds a key value. Values live in their own file in
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

``saved_logins`` holds what may be shown about a saved website login: its
site (an https origin), the username, whether it is out of date, who saved it
and when it was last used. One site and username is one login. Its password
is the value of the key store entry ``saved-login-<id>`` (``infra.saved_logins``).
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

    CREATE TABLE IF NOT EXISTS saved_logins (
        id TEXT PRIMARY KEY,
        site TEXT NOT NULL,
        username TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok', 'out_of_date')),
        saved_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_used_at TEXT,
        last_used_by_thread_id TEXT,
        UNIQUE (site, username)
    );

    CREATE TABLE IF NOT EXISTS saved_login_fill_audit (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        login_id TEXT NOT NULL,
        site TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        result TEXT NOT NULL CHECK (result IN ('filled', 'rejected', 'unknown')),
        item TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_saved_login_fill_audit_login_created
        ON saved_login_fill_audit(login_id, created_at);

    CREATE TABLE IF NOT EXISTS key_write_attempts (
        token_hash TEXT PRIMARY KEY
            REFERENCES auth_tokens(token_hash) ON DELETE CASCADE,
        failures INTEGER NOT NULL DEFAULT 0 CHECK (failures >= 0),
        updated_at TEXT NOT NULL
    );
"""
