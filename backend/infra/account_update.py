"""Changing the account the setup screen collected: name, email and password, never the username."""

from __future__ import annotations

import sqlite3

from infra import user_profile
from infra.accounts import (
    AccountError,
    hash_password,
    normalize_username,
    require_password,
    utc_now_iso,
    write_transaction,
)


def update_account(
    username: str, *, full_name: str | None = None, email: str | None = None,
    password: str | None = None, db: sqlite3.Connection | None = None,
) -> bool:
    """Change what a person gave setup's account screen: their name and email, and their password.

    ``None`` leaves a field alone; an empty ``email`` removes it. The username is the data owner id and never changes.
    The profile write is staged first and published as the transaction's last step, undone if it does not commit.
    Returns True when the password changed, which revokes every session: the caller issues the next one.
    """
    from infra import config

    name = normalize_username(username)
    if full_name is not None and not user_profile.one_line(full_name):
        raise AccountError("A name is required.")
    encoded = None
    if password is not None:
        require_password(password)
        encoded = hash_password(password)
    staged = None
    if full_name is not None or email is not None:
        staged = user_profile.stage_profile_update(config.WORKSPACE, full_name=full_name, email=email)
    now = utc_now_iso()
    try:
        with write_transaction(db, scope="update_account") as conn:
            if not conn.execute("SELECT 1 FROM users WHERE id = ?", (name,)).fetchone():
                raise AccountError(f"no account named '{name}'")
            if encoded is not None:
                conn.execute("UPDATE users SET password_hash = ?, password_changed_at = ? WHERE id = ?", (encoded, now, name))
                conn.execute("DELETE FROM auth_tokens WHERE user_id = ? AND kind = 'session'", (name,))
            if staged:
                staged.publish()
    except BaseException:
        if staged:
            staged.undo()
        raise
    return encoded is not None
