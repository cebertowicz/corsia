"""Select one reachable connected account across one or more providers."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import NamedTuple

from connections.account_errors import AccountError
from connections.connected_accounts import list_accounts, visible_accounts


class Refusals(NamedTuple):
    """The fixed texts one resolver answers with; none echoes the passed name."""

    none_connected: str
    none_available: str
    choose: str
    unknown: str


def _choices(accounts: list[dict]) -> str:
    return ", ".join(f"{account['address']} (account_id {account['id']})" for account in accounts)


def resolve(
    selector: object, thread_id: object, *, providers: tuple[str, ...],
    usable_for: Callable[[dict, sqlite3.Connection], dict], refusals: Refusals,
    error_type: type[AccountError] = AccountError, db: sqlite3.Connection | None = None,
) -> dict:
    """The one reachable account of ``providers`` named by ``selector``, else a fixed refusal."""
    if db is None:
        from infra.database import get_db

        connection = get_db()
        try:
            return resolve(
                selector, thread_id, providers=providers, usable_for=usable_for, refusals=refusals,
                error_type=error_type, db=connection,
            )
        finally:
            connection.close()
    if not any(list_accounts(provider, db=db) for provider in providers):
        raise error_type(refusals.none_connected)
    caller = thread_id if isinstance(thread_id, str) and thread_id else None
    reachable = [
        account for provider in providers if caller is not None
        for account in visible_accounts(caller, db=db, provider=provider)
    ]
    if not reachable:
        raise error_type(refusals.none_available)
    listed = _choices(reachable)
    if selector is None or (isinstance(selector, str) and not selector.strip()):
        if len(reachable) == 1:
            return usable_for(reachable[0], db)
        raise error_type(refusals.choose.format(choices=listed))
    wanted = selector.strip() if isinstance(selector, str) else ""
    named = [account for account in reachable if account["id"] == wanted] or [
        account for account in reachable if str(account["address"]).strip().lower() == wanted.lower()
    ]
    if len(named) != 1:
        raise error_type((refusals.choose if named else refusals.unknown).format(choices=listed))
    return usable_for(named[0], db)
