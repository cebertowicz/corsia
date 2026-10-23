"""Changing the account the setup screen collected: name, email and password, never the username."""

from __future__ import annotations

import sqlite3

from infra import setup_marker, user_profile
from infra.accounts import (
    AccountError,
    issue_token,
    normalize_username,
    require_password,
    set_password,
    write_transaction,
)


def update_account(
    username: str, *, full_name: str | None = None, email: str | None = None,
    password: str | None = None, db: sqlite3.Connection | None = None,
) -> tuple[str, str | None] | None:
    """Change what a person gave setup's account screen: their name and email, and their password.

    ``None`` leaves a field alone; an empty ``email`` removes it. The username is the data owner id and never changes.
    The profile write is staged first and published as the transaction's last step, undone if it does not commit.
    A new password goes through ``set_password``, which owns what a password change revokes (every session), and the
    next session is issued in the same transaction, so a failure leaves the old password and session as they were.
    Returns that new session as ``(token, expires_at)`` when the password changed, else ``None``.
    """
    from infra import config

    name = normalize_username(username)
    if full_name is not None and not user_profile.one_line(full_name):
        raise AccountError("A name is required.")
    if password is not None:
        require_password(password)
    staged = None
    if full_name is not None or email is not None:
        staged = user_profile.stage_profile_update(config.WORKSPACE, full_name=full_name, email=email)
    session = None
    try:
        with write_transaction(db, scope="update_account") as conn:
            if not conn.execute("SELECT 1 FROM users WHERE id = ?", (name,)).fetchone():
                raise AccountError(f"no account named '{name}'")
            if password is not None:
                set_password(name, password, db=conn)
                session = issue_token(name, kind="session", label="browser", db=conn)
            if staged:
                staged.publish()
                # A profile the unfinished-setup marker lists must keep matching its digest, or a relaunch
                # would leave the abandoned setup's name behind; a marker that cannot be updated undoes it.
                setup_marker.update_created_file(user_profile.PROFILE_RELATIVE_PATH.as_posix(), staged.sha256)
    except BaseException:
        if staged:
            staged.undo()
        raise
    return session
