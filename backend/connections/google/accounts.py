"""Which Google account one call uses, checked against the caller's reach on every call.

An agent names the account by ``account_id`` or by the address the person
used. With no name, a caller with one reachable account uses it; otherwise the
call returns a clarification listing the accounts this caller can reach. Every error text
here is fixed and safe to show. Only the accounts that reach the caller are
ever searched, so an out-of-reach account named by id or address gets exactly
the answer a name that matches nothing gets, and a caller that reaches no
account gets ``NONE_AVAILABLE``, which claims nothing about where any account works.
Only when no Google account exists at all does it say to connect one.
"""

from __future__ import annotations

import sqlite3

from connections.account_resolver import Refusals, resolve
from connections.connected_accounts import GOOGLE_PROVIDER
from connections.google.client import (
    NOT_CONNECTED, SIGN_IN_RECOVERY, STATUS_CONNECTED, STATUS_NEEDS_SIGN_IN, GoogleError,
)

NONE_AVAILABLE = (
    "No Google account is available here. If one should be, ask the person to change Where it works "
    "on its row in Connected apps."
)
CHOOSE = (
    "Pass account_id to say which Google account to use: {choices}. If the person hasn't said "
    "which account, ask them; never pick one for them."
)
# Never echoes the name, so the answer is byte-identical whichever unusable name was passed.
UNKNOWN = "No Google account matches that name. Pass account_id as one of: {choices}."
SIGNED_OUT = (
    "{address} is signed out. Ask the person to use Sign in on its row in Connected apps, or call "
    "google_connect with this account_id and post its reply verbatim followed by its link as "
    "[Open it in Connected apps](<link>)."
)
UNAVAILABLE = "{address} isn't available right now. Try again later."


GOOGLE_REFUSALS = Refusals(NOT_CONNECTED, NONE_AVAILABLE, CHOOSE, UNKNOWN)
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


def usable(account: dict) -> dict:
    state = account["state"]
    if state == STATUS_CONNECTED:
        return account
    if state == STATUS_NEEDS_SIGN_IN:
        raise GoogleError(f"{account['address']}: {SIGN_IN_RECOVERY}")
    if state == "signed_out":
        raise GoogleError(SIGNED_OUT.format(address=account["address"]))
    raise GoogleError(UNAVAILABLE.format(address=account["address"]))


def _google_usable(account: dict, db: sqlite3.Connection) -> dict:
    return usable(account)



def resolve_for_thread(selector: object, thread_id: object, *, db: sqlite3.Connection | None = None) -> dict:
    """The one reachable connected Google account ``selector`` names; else a fixed refusal."""
    return resolve(
        selector, thread_id, providers=(GOOGLE_PROVIDER,), usable_for=_google_usable,
        refusals=GOOGLE_REFUSALS, error_type=GoogleError, db=db,
    )
