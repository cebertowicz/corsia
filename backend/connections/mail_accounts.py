"""Which email account one ``email_*`` or ``calendar_list`` call uses, across Gmail and other mail hosts.

The same rules as ``connections.google.accounts``: the caller names the account by ``account_id``
or by address, only accounts that reach the caller are searched, a caller with one reachable
account may omit the name, and every refusal is a fixed text that echoes no input. It is that
resolver, run over Gmail and mail-host accounts.
"""

from __future__ import annotations

import sqlite3

from connections.account_errors import AccountError
from connections.account_resolver import Refusals, resolve
from connections.connected_accounts import GOOGLE_PROVIDER, MAILHOST_PROVIDER
from connections.google import accounts as google_accounts
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
NO_EMAIL_ACCOUNT = (
    "No Gmail or other mail-host account is connected. For Microsoft mail, use microsoft_status, then "
    "microsoft_mail_search or microsoft_mail_read. To connect Gmail or Google Workspace, call google_connect; "
    "any other host follows mail-host-setup."
)
MAIL_ACCOUNT_PARAM = {
    "type": "string",
    "description": (
        "The email account to use: its account_id or the address the person named. Required when the person has "
        "more than one account; with none given, the result lists the accounts you can use."
    ),
}
_BY_STATUS = {
    store.WAITING: "{address} is waiting for an app password. Ask the person to set it on its row in Connected apps.",
    store.CHECKING: "{address} is still being checked. Try again in a minute.",
    store.REFUSED: "{address}'s host didn't accept its app password. Ask the person to make a new one and Replace key on its row in Connected apps.",
    store.REVOKED: "{address}'s host stopped accepting its app password. Ask the person to make a new one and Replace key on its row in Connected apps.",
    store.IMAP_OFF: "{address}'s host has email access for other apps turned off. Ask the person to turn on IMAP there and press Retry.",
    store.UNREACHABLE: "CORSIA can't reach {address}'s mail server. Ask the person to check the server names in Connected apps.",
}


def _mail_host_usable(account: dict, db: sqlite3.Connection) -> dict:
    if account["state"] == "connected":
        return account
    status = store.read(str(account["id"]), db=db)[1].get("status")
    raise AccountError(_BY_STATUS.get(status, UNAVAILABLE).format(address=account["address"]))


def _usable(account: dict, db: sqlite3.Connection) -> dict:
    return _mail_host_usable(account, db) if account["provider"] == MAILHOST_PROVIDER else google_accounts.usable(account, db)


REFUSALS = Refusals(NO_EMAIL_ACCOUNT, NONE_AVAILABLE, CHOOSE, UNKNOWN)


def resolve_for_thread(selector: object, thread_id: object, *, db: sqlite3.Connection | None = None) -> dict:
    """The one reachable connected email account ``selector`` names; else a fixed refusal."""
    return resolve(
        selector, thread_id, providers=(GOOGLE_PROVIDER, MAILHOST_PROVIDER), usable_for=_usable,
        refusals=REFUSALS, db=db,
    )
