"""Accounts DDL — the two tables behind every authenticated request.

Lives beside ``infra.schema`` rather than inside it for the same reason plans,
messages and usage do: the schema owner composes one ``SCHEMA_SQL`` from
domain-owned constants, and this domain owns its own.

``users.id`` is the username *and* the data-owner id that ``threads.user_id``
already holds, so an install that predates accounts keeps its data by choosing
the same name. ``auth_tokens`` stores a token only as its SHA-256; the
plaintext exists once, at issue. ``ON DELETE CASCADE`` means deleting an
account takes its credentials with it rather than leaving orphans that
resolve to a user who no longer exists.
"""

from __future__ import annotations

ACCOUNTS_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        password_changed_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS auth_tokens (
        token_hash TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('session', 'api', 'service')),
        label TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_auth_tokens_user
        ON auth_tokens(user_id, created_at DESC);

    CREATE INDEX IF NOT EXISTS idx_auth_tokens_expiry
        ON auth_tokens(expires_at) WHERE expires_at IS NOT NULL;
"""
