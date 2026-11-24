"""Record the result of a saved-login fill without reading its password."""

from infra import saved_logins
from infra.database import write_transaction
from infra.key_store_values import KeyStoreError
from infra.time import utc_now_iso

FILLED = "Signed in to {site} as {username} with the saved login."
REJECTED = "The saved login for {site} ({username}) didn't work, so it's now marked out of date. The person has a step to sign in to {site} themselves."


def _record(thread_id: str, login_id: str, *, succeeded: bool) -> str:
    if not isinstance(thread_id, str) or not thread_id:
        raise KeyStoreError("A login's use needs the thread that used it.")
    with write_transaction(None, scope="saved_login_fill_result") as conn:
        login = saved_logins.get_login(login_id, db=conn)
        if succeeded:
            saved_logins.record_use(login_id, thread_id, db=conn)
        else:
            saved_logins.mark_out_of_date(login_id, db=conn)
        conn.execute(
            "INSERT INTO saved_login_fill_audit (created_at, login_id, site, username, thread_id, result) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (utc_now_iso(), login.id, login.site, login.username, thread_id, "filled" if succeeded else "rejected"),
        )
    return (FILLED if succeeded else REJECTED).format(site=login.site, username=login.username)


def fill_succeeded(thread_id: str, login_id: str) -> str:
    """Record a successful fill and return the agent-facing result."""
    return _record(thread_id, login_id, succeeded=True)


def fill_failed(thread_id: str, login_id: str) -> str:
    """Record a rejected fill and mark the login out of date."""
    return _record(thread_id, login_id, succeeded=False)
