"""Which email account one ``email_*`` or ``calendar_list`` call uses, across Gmail and other mail hosts.

The same rules as ``connections.google.accounts``: the caller names the account by ``account_id``
or by address, only accounts that reach the caller are searched, a caller with one reachable
account may omit the name, and every refusal is a fixed text that echoes no input. With no
mail-host account on this install it is that Google resolver, unchanged.
"""

from __future__ import annotations

import sqlite3

from connections.connected_accounts import GOOGLE_PROVIDER, MAILHOST_PROVIDER, list_accounts, visible_accounts
from connections.google import accounts as google_accounts
from connections.google.client import GoogleError
from connections.mailhost import store

NONE_AVAILABLE = (
    "No email account is available here. If one should be, ask the person to change Where it works "
    "on its row in Connected apps."
)
CHOOSE = (
    "Pass account_id to say which account to use: {choices}. If the person hasn't said "
    "which account, ask them; never pick one for them."
)
UNKNOWN = "No account matches that name. Pass account_id as one of: {choices}."
UNAVAILABLE = "{address} isn't available right now. Try again later."
_BY_STATUS = {
    store.WAITING: "{address} is waiting for an app password. Ask the person to set it in Settings › Keys.",
    store.CHECKING: "{address} is still being checked. Try again in a minute.",
    store.REFUSED: "{address}'s host didn't accept its app password. Ask the person to make a new one and replace the key in Settings › Keys.",
    store.IMAP_OFF: "{address}'s host has email access for other apps turned off. Ask the person to turn on IMAP there and press Try again.",
    store.UNREACHABLE: "CORSIA can't reach {address}'s mail server. Ask the person to check the server names in Connected apps.",
}


def _mail_host_usable(account: dict, db: sqlite3.Connection) -> dict:
    if account["state"] == "connected":
        return account
    status = store.read(str(account["id"]), db=db)[1].get("status")
    raise GoogleError(_BY_STATUS.get(status, UNAVAILABLE).format(address=account["address"]))


def _usable(account: dict, db: sqlite3.Connection) -> dict:
    return _mail_host_usable(account, db) if account["provider"] == MAILHOST_PROVIDER else google_accounts.usable(account)


def resolve_for_thread(selector: object, thread_id: object, *, db: sqlite3.Connection | None = None) -> dict:
    """The one reachable connected email account ``selector`` names; else a fixed refusal."""
    if db is None:
        from infra.database import get_db

        connection = get_db()
        try:
            return resolve_for_thread(selector, thread_id, db=connection)
        finally:
            connection.close()
    if not list_accounts(MAILHOST_PROVIDER, db=db):
        return google_accounts.resolve_for_thread(selector, thread_id, db=db)
    caller = thread_id if isinstance(thread_id, str) and thread_id else None
    reachable = [
        account for provider in (GOOGLE_PROVIDER, MAILHOST_PROVIDER) if caller is not None
        for account in visible_accounts(caller, db=db, provider=provider)
    ]
    if not reachable:
        raise GoogleError(NONE_AVAILABLE)
    listed = google_accounts.choices(reachable)
    if selector is None or (isinstance(selector, str) and not selector.strip()):
        if len(reachable) == 1:
            return _usable(reachable[0], db)
        raise GoogleError(CHOOSE.format(choices=listed))
    wanted = selector.strip() if isinstance(selector, str) else ""
    named = [account for account in reachable if account["id"] == wanted] or [
        account for account in reachable if str(account["address"]).strip().lower() == wanted.lower()
    ]
    if len(named) != 1:
        raise GoogleError(CHOOSE.format(choices=listed) if named else UNKNOWN.format(choices=listed))
    return _usable(named[0], db)
