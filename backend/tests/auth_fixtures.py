"""The one place a test gets a credential.

Before accounts existed, every test that called an authenticated route pasted
one of two real bearer tokens belonging to real people, and a handful more
replaced ``auth.USERS`` with a dict of their own. Both are gone: tokens are
rows now, and a token that is not in the database does not authenticate.

So tests seed a row instead. That is what this module does, and the values it
seeds are deliberately neutral — nobody's name, nothing that would still work
against a real install.

``seed_account`` writes the stored form directly rather than calling
:func:`infra.accounts.issue_token`, and that is the point of the store's
design rather than a hole in it: issuing returns a *random* token exactly once,
so no caller can ask for a known one. A test needs a known one, and building it
from the store's own :func:`infra.accounts.token_digest` keeps the two in step
— if the stored form ever changes, this follows it.
"""

from __future__ import annotations

import sqlite3

from infra.accounts import token_digest
from infra.accounts_schema import ACCOUNTS_SCHEMA_SQL
from infra.database import write_transaction
from infra.passwords import hash_password
from infra.time import utc_now_iso

#: The data owner most tests act as. Its id is still ``steve`` because existing
#: rows across the suite are keyed to it; only the credential is neutral.
OWNER_USER = "steve"
OWNER_TOKEN = "test-owner-token"

#: A known browser-session credential for routes that record a human action.
OWNER_SESSION_TOKEN = "test-owner-session-token"

#: A second account, for the tests that prove one user cannot reach another's
#: threads, artifacts or leases.
OTHER_USER = "leksi"
OTHER_TOKEN = "test-other-token"

#: The password behind both accounts, for tests that actually sign in.
TEST_PASSWORD = "test-password-9876"

_encoded_password: str | None = None


def seeded_password_hash() -> str:
    """Return one scrypt hash of :data:`TEST_PASSWORD`, computed once.

    Hashing costs ~128 MB and ~0.3 s by design. Every seeded account can share
    one hash of the same password, so a test session pays that once.
    """
    global _encoded_password
    if _encoded_password is None:
        _encoded_password = hash_password(TEST_PASSWORD)
    return _encoded_password


def seed_account(
    user_id: str,
    token: str,
    *,
    kind: str = "api",
    db: sqlite3.Connection | None = None,
) -> None:
    """Give ``user_id`` an account and a known token in the current database.

    Idempotent: seeding an account that already exists leaves it alone, so a
    session-wide seed and a per-test one cannot collide.
    """
    now = utc_now_iso()
    with write_transaction(db, scope="seed_test_account") as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users"
            " (id, password_hash, created_at, password_changed_at)"
            " VALUES (?, ?, ?, ?)",
            (user_id, seeded_password_hash(), now, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO auth_tokens"
            " (token_hash, user_id, kind, label, created_at, expires_at)"
            " VALUES (?, ?, ?, 'test', ?, NULL)",
            (token_digest(token), user_id, kind, now),
        )


def seed_test_accounts(db: sqlite3.Connection | None = None) -> None:
    """Seed the owner and second accounts every authenticated test relies on."""
    seed_account(OWNER_USER, OWNER_TOKEN, db=db)
    seed_account(OWNER_USER, OWNER_SESSION_TOKEN, kind="session", db=db)
    seed_account(OTHER_USER, OTHER_TOKEN, db=db)


def create_accounts_tables(db: sqlite3.Connection) -> None:
    """Add the account tables to a fixture database built table by table.

    Some fixtures hand-write only the handful of tables their test needs
    instead of running the whole schema. Authentication is not optional for
    any of them: a route resolves its caller against these tables, so a
    fixture database without them cannot serve an authenticated request.

    Patching ``infra.auth.authenticate`` instead is a trap. Modules bind it
    with ``from infra.auth import authenticate`` at import time, so a module
    first imported while a patch is installed keeps the patched function for
    the rest of the session, and later tests fail in a different file.
    """
    db.executescript(ACCOUNTS_SCHEMA_SQL)
