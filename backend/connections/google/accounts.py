"""Which Google account one call uses, checked against the caller's reach on every call.

An agent names the account by ``account_id`` or by the address the person
used. There is never a default: with no name, the call returns a
clarification listing the accounts this caller can reach. Every error text
here is fixed and safe to show. An address the caller names that is connected but out of
reach gets the reach refusal, which says the account exists and who can widen it; an
address no account has gets "No Google account matches". An out-of-reach account is
never listed in a clarification.
"""

from __future__ import annotations

import sqlite3

from connections.connected_accounts import account_reaches_thread, google_accounts
from connections.google.client import (
    NOT_CONNECTED, SIGN_IN_RECOVERY, STATUS_CONNECTED, STATUS_NEEDS_SIGN_IN, GoogleError,
)

REACH_REFUSAL = (
    "This Google account is available only in CORSIA. Ask the person who connected it to change "
    "Where it works."
)
CHOOSE = (
    "Pass account_id to say which Google account to use: {choices}. If the person hasn't said "
    "which account, ask them; never pick one for them."
)
UNKNOWN = "No Google account matches {selector!r}. Pass account_id as one of: {choices}."
SIGNED_OUT = (
    "{address} is signed out. Ask the person to use Sign in on its row in Connected apps, or call "
    "google_connect with this account_id and post its reply verbatim followed by its link as "
    "[Open it in Connected apps](<link>)."
)
UNAVAILABLE = "{address} isn't available right now. Try again later."
ACCOUNT_PARAM = {
    "type": "string",
    "description": (
        "The Google account to use: its account_id from google_status, or the address the person "
        "named. Required when the person has more than one account; with none given, the result "
        "lists the accounts you can use."
    ),
}


def thread_key(account_id: str, gmail_thread_id: object) -> str | None:
    """The CORSIA email-thread id: Gmail's thread id is local to one mailbox, so it is namespaced."""
    if not isinstance(gmail_thread_id, str) or not gmail_thread_id:
        return None
    return f"google:{account_id}:thread:{gmail_thread_id}"


def _choices(accounts: list[dict]) -> str:
    return ", ".join(f"{account['address']} (account_id {account['id']})" for account in accounts)


def _usable(account: dict) -> dict:
    state = account["state"]
    if state == STATUS_CONNECTED:
        return account
    if state == STATUS_NEEDS_SIGN_IN:
        raise GoogleError(f"{account['address']}: {SIGN_IN_RECOVERY}")
    if state == "signed_out":
        raise GoogleError(SIGNED_OUT.format(address=account["address"]))
    raise GoogleError(UNAVAILABLE.format(address=account["address"]))


def visible_google_accounts(thread_id: str | None, *, db: sqlite3.Connection) -> list[dict[str, object]]:
    """Every Google account for the person (``thread_id`` None); for an agent, only those that reach its thread."""
    return [
        account for account in google_accounts(db=db)
        if thread_id is None or account_reaches_thread(str(account["id"]), thread_id, db=db)
    ]


def resolve_for_thread(selector: object, thread_id: object, *, db: sqlite3.Connection | None = None) -> dict:
    """The one connected account ``selector`` names, if ``thread_id`` may use it; else a fixed refusal."""
    if db is None:
        from infra.database import get_db

        connection = get_db()
        try:
            return resolve_for_thread(selector, thread_id, db=connection)
        finally:
            connection.close()
    accounts = google_accounts(db=db)
    if not accounts:
        raise GoogleError(NOT_CONNECTED)
    if not isinstance(thread_id, str) or not thread_id:
        raise GoogleError(REACH_REFUSAL)
    reachable = [account for account in accounts if account_reaches_thread(str(account["id"]), thread_id, db=db)]
    if selector is None or (isinstance(selector, str) and not selector.strip()):
        raise GoogleError(CHOOSE.format(choices=_choices(reachable)) if reachable else REACH_REFUSAL)
    wanted = selector.strip() if isinstance(selector, str) else ""
    named = [account for account in accounts if account["id"] == wanted] or [
        account for account in accounts if str(account["address"]).strip().lower() == wanted.lower()
    ]
    if len(named) != 1:
        if not reachable:
            raise GoogleError(REACH_REFUSAL)
        listed = _choices(reachable)
        raise GoogleError(CHOOSE.format(choices=listed) if named else UNKNOWN.format(selector=wanted, choices=listed))
    if named[0] not in reachable:
        raise GoogleError(REACH_REFUSAL)
    return _usable(named[0])
