"""Accounts and auth tokens — the store behind every authenticated request.

This replaces a hardcoded dict of two people and their permanent plaintext
tokens. Three properties come from that history and are load-bearing.

**The username is the data owner id.** ``threads.user_id``,
``host_access_leases.user_id`` and the rest already hold ``steve``; making the
account id the same string means an existing install keeps its data by choosing
the same name, with no row rewrite and no id mapping table.

**A token is stored only as its SHA-256.** The plaintext is shown once, at
issue. A database copy, a backup or a log line therefore cannot be replayed as
a credential. SHA-256 without a salt or a work factor is right *here* and wrong
for passwords: a token is 256 bits of ``secrets`` output, so there is no
guessable space to grind, and this runs on every request.

**Expiry is enforced at resolution, not by a sweep.** A background cleaner that
falls behind, or does not run, would silently extend every session it missed.
:func:`resolve_token` compares against the clock on each lookup, so an expired
token is dead the moment it expires whether or not anything swept it.

Kinds: ``session`` (issued by login, 30 days), ``api`` (issued by the CLI for
agent tooling, no expiry, revocable by label), ``service`` (minted by the
lifecycle shell for its own loopback calls, one hour).
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from datetime import timedelta

from infra.database import get_db, write_transaction
from infra.passwords import MIN_PASSWORD_LENGTH, hash_password, verify_password
from infra.time import utc_now, utc_now_iso

#: Lowercase, so a username cannot differ from a data-owner id by case alone.
USERNAME_PATTERN = re.compile(r"^[a-z0-9_-]{1,32}$")

TOKEN_KINDS = ("session", "api", "service")
SESSION_TTL = timedelta(days=30)
SERVICE_TTL = timedelta(hours=1)


class AccountError(ValueError):
    """A request about an account is invalid — safe to show the operator."""


class FirstAccountExists(AccountError):
    """A first-run account request lost to an account already in the store."""


_UNKNOWN_ACCOUNT_HASH: str | None = None


def _unknown_account_hash() -> str:
    """Return a hash to verify against when the username does not exist.

    Created once per process from a random value nobody knows, so the check
    always fails but costs the same scrypt work as a real account's.
    """
    global _UNKNOWN_ACCOUNT_HASH
    if _UNKNOWN_ACCOUNT_HASH is None:
        _UNKNOWN_ACCOUNT_HASH = hash_password(secrets.token_urlsafe(32))
    return _UNKNOWN_ACCOUNT_HASH


def _db(db: sqlite3.Connection | None) -> sqlite3.Connection:
    """Return the caller's connection, or this thread's pooled one."""
    return db if db is not None else get_db()


def token_digest(token: str) -> str:
    """Return the stored form of ``token`` — its SHA-256, hex encoded."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_username(raw: str) -> str:
    """Lowercase and validate ``raw``, or raise :class:`AccountError`."""
    name = (raw or "").strip().lower()
    if not USERNAME_PATTERN.match(name):
        raise AccountError(
            "username must be 1-32 characters of a-z, 0-9, '-' or '_'"
        )
    return name


def _require_password(password: str) -> None:
    """Reject a password too short to be worth hashing."""
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AccountError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )


def has_users(db: sqlite3.Connection | None = None) -> bool:
    """True iff at least one account exists."""
    return _db(db).execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def get_user(user_id: str, db: sqlite3.Connection | None = None) -> dict | None:
    """Return ``{id, name}`` for ``user_id``, or None."""
    row = _db(db).execute(
        "SELECT id FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return {"id": row["id"], "name": row["id"]} if row else None


def list_users(db: sqlite3.Connection | None = None) -> list[dict]:
    """Return every account, oldest first, without any secret material."""
    rows = _db(db).execute(
        "SELECT id, created_at, password_changed_at FROM users"
        " ORDER BY created_at, id"
    ).fetchall()
    return [dict(row) for row in rows]


def data_owners_without_accounts(
    db: sqlite3.Connection | None = None,
) -> list[str]:
    """Return user ids that own threads but have no account.

    An install that predates accounts has rows owned by whatever id the old
    hardcoded table used. Creating an account under a *different* name would
    leave that data unreachable, so first-run setup shows these names and says
    to pick one to keep it.
    """
    rows = _db(db).execute(
        "SELECT DISTINCT t.user_id FROM threads t"
        " LEFT JOIN users u ON u.id = t.user_id"
        " WHERE u.id IS NULL AND t.user_id IS NOT NULL AND t.user_id != ''"
        " ORDER BY t.user_id"
    ).fetchall()
    return [row["user_id"] for row in rows]


def create_user(
    username: str, password: str, *, db: sqlite3.Connection | None = None,
    first_run_wake: bool = True, require_first: bool = False,
    claim_data_owner: bool = False, profile_seed: tuple[str, str] | None = None,
) -> str:
    """Create an account and return its id. Raises if the name is taken.

    The first account also gets the workspace conductor; ``first_run_wake``
    queues that conductor's first-run wake in the same transaction. Only a
    caller with no person waiting on the conductor (the eval harness)
    passes False. ``require_first`` atomically refuses if another account
    already exists, for pre-auth first-run setup. ``claim_data_owner`` and
    ``profile_seed`` are the first-account rules; use
    :func:`infra.first_account.create_first_account`.
    """
    name = normalize_username(username)
    _require_password(password)
    encoded = hash_password(password)
    now = utc_now_iso()
    woke = False
    with write_transaction(db, scope="create_user") as conn:
        had_users = has_users(conn)
        if require_first and had_users:
            raise FirstAccountExists("setup is already complete")
        if claim_data_owner:
            from infra import first_account

            first_account.require_data_owner_name(name, conn)
        if conn.execute("SELECT 1 FROM users WHERE id = ?", (name,)).fetchone():
            raise AccountError(f"account '{name}' already exists")
        conn.execute(
            "INSERT INTO users (id, password_hash, created_at, password_changed_at)"
            " VALUES (?, ?, ?, ?)",
            (name, encoded, now, now),
        )
        if not had_users:
            if profile_seed is not None:
                from infra import first_account

                first_account.seed_workspace_profile(*profile_seed)
            from infra.conductor import WORKSPACE_SCOPE, ensure_scope_conductor

            conductor_id = ensure_scope_conductor(
                WORKSPACE_SCOPE, name, model=None, db=conn,
            )
            if not data_owners_without_accounts(conn):
                from infra import config
                from registry.shared_ownership_assignment import enforce_scaffold_ownership

                enforce_scaffold_ownership(config.WORKSPACE, None, db=conn)
            if first_run_wake and conductor_id:
                from infra.onboarding_events import (
                    enqueue_first_run_wake, seed_first_run_setup_plan,
                )

                seed_first_run_setup_plan(conductor_id, name, db=conn)
                woke = enqueue_first_run_wake(conductor_id, db=conn)
    if not had_users:
        from infra import onboarding_events

        if woke:
            onboarding_events.start_first_run_task(conductor_id, name)
        onboarding_events.emit("account_created")
        if woke:
            onboarding_events.emit("first_run_wake")
    return name


def set_password(
    username: str, password: str, *, db: sqlite3.Connection | None = None
) -> int:
    """Replace ``username``'s password and revoke its sessions.

    API tokens deliberately survive: they belong to tooling the operator
    installed, not to a browser whose credential may have leaked. Returns the
    number of sessions revoked.
    """
    name = normalize_username(username)
    _require_password(password)
    encoded = hash_password(password)
    now = utc_now_iso()
    with write_transaction(db, scope="set_password") as conn:
        if not conn.execute(
            "SELECT 1 FROM users WHERE id = ?", (name,)
        ).fetchone():
            raise AccountError(f"no account named '{name}'")
        conn.execute(
            "UPDATE users SET password_hash = ?, password_changed_at = ?"
            " WHERE id = ?",
            (encoded, now, name),
        )
        cursor = conn.execute(
            "DELETE FROM auth_tokens WHERE user_id = ? AND kind = 'session'",
            (name,),
        )
        return cursor.rowcount or 0


def check_password(
    username: str, password: str, *, db: sqlite3.Connection | None = None
) -> dict | None:
    """Return ``{id, name}`` iff the credentials are valid, else None.

    One return value for "no such account" and "wrong password", so the caller
    cannot turn the difference into a username oracle.
    """
    try:
        name = normalize_username(username)
    except AccountError:
        return None
    row = _db(db).execute(
        "SELECT id, password_hash FROM users WHERE id = ?", (name,)
    ).fetchone()
    if row is None:
        # Same work as a wrong password, so response time does not reveal
        # which usernames exist.
        verify_password(password, _unknown_account_hash())
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return {"id": row["id"], "name": row["id"]}


def lifecycle_token_label(operation_id: str) -> str:
    """Build the server-owned label for one lifecycle operation."""
    return f"lifecycle:{operation_id}"


def issue_service_token(user_id: str, label: str) -> str:
    """Mint one short-lived lifecycle credential for an explicit account."""
    token, _expires_at = issue_token(user_id, kind="service", label=label)
    return token


def issue_lifecycle_service_token(user_id: str, operation_id: str) -> str:
    """Mint the one-hour drain credential for one server-created operation."""
    return issue_service_token(user_id, lifecycle_token_label(operation_id))


def revoke_lifecycle_service_token(
    user_id: str, operation_id: str, *, db: sqlite3.Connection | None = None
) -> int:
    """Revoke the service credential attached to one settled lifecycle operation."""
    return revoke_label(user_id, lifecycle_token_label(operation_id), db=db)


def issue_token(
    user_id: str,
    *,
    kind: str = "session",
    label: str | None = None,
    db: sqlite3.Connection | None = None,
) -> tuple[str, str | None]:
    """Mint a token for ``user_id`` and return ``(plaintext, expires_at)``.

    The plaintext is returned exactly once and never stored; only its digest
    goes to the database. ``expires_at`` is None for an ``api`` token.

    Issuing is also when expired rows leave the table. Tying the cleanup to
    the one write that grows it needs no scheduler and cannot fall behind;
    :func:`resolve_token` already refuses an expired token either way, so this
    is housekeeping and never the thing that makes expiry true.
    """
    if kind not in TOKEN_KINDS:
        raise AccountError(f"unknown token kind: {kind!r}")
    ttl = {"session": SESSION_TTL, "service": SERVICE_TTL}.get(kind)
    expires_at = (utc_now() + ttl).isoformat() if ttl else None
    token = secrets.token_urlsafe(32)
    with write_transaction(db, scope="issue_token") as conn:
        purge_expired_tokens(conn)
        if not conn.execute(
            "SELECT 1 FROM users WHERE id = ?", (user_id,)
        ).fetchone():
            raise AccountError(f"no account named '{user_id}'")
        conn.execute(
            "INSERT INTO auth_tokens"
            " (token_hash, user_id, kind, label, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                token_digest(token), user_id, kind, label,
                utc_now_iso(), expires_at,
            ),
        )
    return token, expires_at


def resolve_token(
    token: str | None, db: sqlite3.Connection | None = None
) -> dict | None:
    """Resolve a credential to its user and token kind, or None if expired.

    Keep credential provenance so human-only decisions cannot mistake an API
    or service credential for a browser session. No credential value escapes.
    """
    if not token:
        return None
    row = _db(db).execute(
        "SELECT user_id, kind, expires_at FROM auth_tokens WHERE token_hash = ?",
        (token_digest(token),),
    ).fetchone()
    if row is None:
        return None
    if row["expires_at"] and row["expires_at"] <= utc_now_iso():
        return None
    return {
        "id": row["user_id"], "name": row["user_id"], "token_kind": row["kind"],
    }


def revoke_token(
    token: str, *, db: sqlite3.Connection | None = None
) -> bool:
    """Delete exactly this token. True iff it existed."""
    with write_transaction(db, scope="revoke_token") as conn:
        cursor = conn.execute(
            "DELETE FROM auth_tokens WHERE token_hash = ?", (token_digest(token),)
        )
        return bool(cursor.rowcount)


def revoke_label(
    user_id: str, label: str, *, db: sqlite3.Connection | None = None
) -> int:
    """Delete ``user_id``'s tokens carrying ``label``. Returns the count."""
    with write_transaction(db, scope="revoke_label") as conn:
        cursor = conn.execute(
            "DELETE FROM auth_tokens WHERE user_id = ? AND label = ?",
            (user_id, label),
        )
        return cursor.rowcount or 0


def list_tokens(
    user_id: str | None = None, db: sqlite3.Connection | None = None
) -> list[dict]:
    """List tokens (never their values) for one user, or for everyone."""
    sql = (
        "SELECT user_id, kind, label, created_at, expires_at FROM auth_tokens"
    )
    params: tuple = ()
    if user_id:
        sql += " WHERE user_id = ?"
        params = (user_id,)
    sql += " ORDER BY created_at DESC"
    return [dict(row) for row in _db(db).execute(sql, params).fetchall()]


def purge_expired_tokens(db: sqlite3.Connection | None = None) -> int:
    """Delete tokens already past their expiry. Housekeeping, not enforcement.

    :func:`resolve_token` already refuses an expired token, so this only keeps
    the table from growing without bound.
    """
    with write_transaction(db, scope="purge_expired_tokens") as conn:
        cursor = conn.execute(
            "DELETE FROM auth_tokens"
            " WHERE expires_at IS NOT NULL AND expires_at <= ?",
            (utc_now_iso(),),
        )
        return cursor.rowcount or 0
