"""Record the result of a saved-login fill without reading its password."""

from typing import Literal

from infra import saved_login_fills, saved_logins
from infra.database import write_transaction
from infra.key_store_values import KeyStoreError
from infra.time import utc_now_iso

FILLED = "Signed in to {site} as {username} with the saved login."
REJECTED = "The saved login for {site} ({username}) didn't work, so it's now marked out of date. The person has a step to sign in to {site} themselves."


def _record(thread_id: str, login_id: str, result: Literal["filled", "rejected", "unknown"]) -> saved_logins.SavedLogin:
    if not isinstance(thread_id, str) or not thread_id:
        raise KeyStoreError("A login's use needs the thread that used it.")
    with write_transaction(None, scope="saved_login_fill_result") as conn:
        login = saved_logins.get_login(login_id, db=conn)
        if result == "filled":
            saved_logins.record_use(login_id, thread_id, db=conn)
        elif result == "rejected":
            saved_logins.mark_out_of_date(login_id, db=conn)
        conn.execute(
            "INSERT INTO saved_login_fill_audit (created_at, login_id, site, thread_id, result) "
            "VALUES (?, ?, ?, ?, ?)",
            (utc_now_iso(), login.id, login.site, thread_id, result),
        )
    return login


def fill_succeeded(thread_id: str, login_id: str) -> str:
    """Record a successful fill and return the agent-facing result."""
    login = _record(thread_id, login_id, "filled")
    return FILLED.format(site=login.site, username=login.username)


def fill_failed(thread_id: str, login_id: str) -> str:
    """Record a rejected fill and mark the login out of date, ending its grants so a re-save can't revive one."""
    login = _record(thread_id, login_id, "rejected")
    saved_login_fills.end_for_login(login.id, deleted=False)
    return REJECTED.format(site=login.site, username=login.username)


def fill_unknown(thread_id: str, login_id: str) -> None:
    """Record a fill whose outcome is unknown, leaving login state unchanged."""
    _record(thread_id, login_id, "unknown")


def item_fill_recorded(thread_id: str, item: str, site: str, result: Literal["filled", "unknown"]) -> None:
    """Record a saved item's fill in the same history: the item's entry name, never its value.

    ``filled`` once the page kept the value; ``unknown`` when it may be in the page but that wasn't confirmed.
    The row's ``login_id`` is empty: it names no login.
    """
    if not isinstance(thread_id, str) or not thread_id:
        raise KeyStoreError("A saved item's use needs the thread that used it.")
    with write_transaction(None, scope="saved_item_fill_result") as conn:
        conn.execute(
            "INSERT INTO saved_login_fill_audit (created_at, login_id, item, site, thread_id, result) "
            "VALUES (?, '', ?, ?, ?, ?)",
            (utc_now_iso(), item, site, thread_id, result),
        )
