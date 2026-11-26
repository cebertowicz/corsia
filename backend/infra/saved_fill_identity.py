"""Who asks for a saved fill, and the refusals a fill request or decision raises.

Shared by the saved-login and saved-item fill approvals, which re-export what
they use, and by scripts that use saved items (``automation.script_items``):
:func:`usable_login` is the one check both run on a saved login.

Thread identity (C3): the thread id comes from the call's server-side context,
never a tool argument; inside an agent's turn any other thread is refused.
"""

from __future__ import annotations

_NO_THREAD = "A fill request needs the agent's own thread."
_OTHER_THREAD = "A fill request can only be made for the agent's own thread."
_AGENT_FALLBACK = "An agent"
_OUT_OF_DATE = "The saved login for {site} ({username}) is out of date. The person has a step to sign in to {site} themselves."
_NOT_THEIRS = "No saved login you may use has that id."


class FillApprovalError(ValueError):
    """A fill request or decision was refused. The text is safe to show: at most a site, a username or an item's
    label, never a password or a value."""


class FillApprovalNotFound(FillApprovalError):
    """No such request or grant for this person."""


class FillApprovalNotTheirs(FillApprovalError):
    """The login isn't the caller's person's: answered as no such login, with nothing about the login itself."""


class FillDurationError(FillApprovalError):
    """The duration is not a whole number of seconds within the cap; nothing was granted."""


def context_thread() -> str | None:
    from infra import state

    return state._current_thread_id.get(None)


def own_thread(thread_id: object) -> str:
    """The thread id, refused when it is empty or names a thread other than the running turn's own."""
    if not isinstance(thread_id, str) or not thread_id:
        raise FillApprovalError(_NO_THREAD)
    running = context_thread()
    if running is not None and running != thread_id:
        raise FillApprovalError(_OTHER_THREAD)
    return thread_id


def thread_record(thread_id: str) -> tuple[str, str, str]:
    """The thread's owner, its visible root and the agent's name, from the server's own records."""
    from core.thread_identity import root_visible_thread_id
    from infra.database import get_db

    db = get_db()
    try:
        row = db.execute("SELECT user_id, title FROM threads WHERE id = ?", (thread_id,)).fetchone()
        if row is None:
            raise FillApprovalError(_NO_THREAD)
        root = root_visible_thread_id(thread_id, db=db) or thread_id
    finally:
        db.close()
    title = row["title"].strip() if isinstance(row["title"], str) else ""
    return row["user_id"], root, title or _AGENT_FALLBACK


def usable_login(thread_id: str, login_id: str):
    """The saved login the thread may use, and the thread's record; refused when it is missing, saved by
    another person, or out of date; and when ``thread_id`` isn't the caller's own (:func:`own_thread`)."""
    from infra import saved_logins
    from infra.key_store_values import KeyStoreError

    clean_thread = own_thread(thread_id)
    try:
        login = saved_logins.get_login(login_id)
    except KeyStoreError as exc:
        raise FillApprovalError(str(exc)) from None
    record = thread_record(clean_thread)
    if login.saved_by != record[0]:
        raise FillApprovalNotTheirs(_NOT_THEIRS)
    if login.status != saved_logins.STATUS_OK:
        raise FillApprovalError(_OUT_OF_DATE.format(site=login.site, username=login.username))
    return login, record
