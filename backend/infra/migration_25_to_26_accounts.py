"""Schema 25 -> 26: accounts and auth tokens.

Before this step a user was a literal entry in a Python dict, so an existing
database has no account at all. The step is therefore purely additive: two new
tables and their indexes, no data to convert and nothing to drop. An install
that migrates has zero accounts afterwards and says so on the sign-in screen;
``corsia user add`` is what creates the first one, and choosing the same name
the install's threads already carry keeps that data.

The DDL is copied rather than imported from ``infra.accounts_schema`` so this
step keeps producing the shape it was written for, exactly as the steps before
it do: importing the live constant would make a database migrated today differ
from one migrated after the next edit to that file.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger(__name__)

_ACCOUNTS_26_DDL = (
    """
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        password_changed_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS auth_tokens (
        token_hash TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('session', 'api', 'service')),
        label TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_auth_tokens_user"
    " ON auth_tokens(user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_auth_tokens_expiry"
    " ON auth_tokens(expires_at) WHERE expires_at IS NOT NULL",
)


def migrate_25_to_26(conn: sqlite3.Connection) -> None:
    """Create the accounts and auth-token tables on an existing database."""
    for statement in _ACCOUNTS_26_DDL:
        conn.execute(statement)
    log.info("Accounts and auth tokens are available; no account exists yet")
