"""The one way the first account comes into existence.

``corsia user add`` and the pre-auth ``/api/setup/first-account`` route both
create it through :func:`create_first_account`, so the data-owner rule, the
workspace profile seeding and the failure policy exist once.
"""

from __future__ import annotations

import sqlite3

from infra.accounts import (
    AccountError, create_user, data_owners_without_accounts, has_users,
)
from infra.database import get_db


class DataOwnerMismatch(AccountError):
    """The first account is not named after any existing data owner."""


class ProfileSeedError(Exception):
    """The workspace profile could not be written, so no account was created."""


def require_data_owner_name(name: str, db: sqlite3.Connection | None = None) -> None:
    """Refuse a first account whose name matches none of the existing data owners.

    The first account on an install that already has threads decides who can
    reach them; any other name would strand every existing thread. A no-op once
    an account exists or when no thread is unowned.
    """
    conn = db if db is not None else get_db()
    if has_users(conn):
        return
    owners = data_owners_without_accounts(conn)
    if owners and name not in owners:
        raise DataOwnerMismatch(
            f"'{name}' owns none of the existing data. Create the first "
            f"account as one of: {', '.join(owners)}. Add others after."
        )


def seed_workspace_profile(call_name: str, email: str = "") -> None:
    """Create the workspace profile from the answers and the Mac's timezone, if missing.

    The one copy both first-account paths use. An existing profile is never
    rewritten; it only gains an email it names none of. Raises
    :class:`ProfileSeedError` when no profile exists afterwards, because the
    writers report an unwritable folder as "not created" and nothing else.
    """
    from infra import config
    from infra.user_profile import (
        PROFILE_RELATIVE_PATH, add_profile_email, mac_timezone, write_user_profile,
    )

    safe_name = "".join(character for character in call_name if character.isprintable())
    if not write_user_profile(config.WORKSPACE, safe_name, mac_timezone(), email):
        add_profile_email(config.WORKSPACE, email)
    if not (config.WORKSPACE / PROFILE_RELATIVE_PATH).is_file():
        raise ProfileSeedError("the workspace profile could not be written")


def create_first_account(
    username: str, password: str, *, call_name: str | None = None, email: str = "",
    require_first: bool = False, first_run_wake: bool = True,
) -> str:
    """Create the install's first account; the one owner behind CLI and setup screen.

    Enforces the data-owner rule and, when ``call_name`` is given, seeds the
    workspace profile in the same transaction as the account: a failure of
    either, including a profile that cannot be written, leaves no account
    behind and raises to the caller.
    """
    return create_user(
        username, password, first_run_wake=first_run_wake, require_first=require_first,
        claim_data_owner=True,
        profile_seed=None if call_name is None else (call_name, email),
    )
