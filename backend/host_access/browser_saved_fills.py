"""Which documents and sessions a saved login was filled in; browser_control applies the limits they carry."""
from __future__ import annotations

# After a saved-login fill, the session runs only browser_session_commands._saved_command shapes until it ends.
SAVED_FILL_SESSION_REFUSED = (
    "A saved login was filled in this browser session, so until the session ends only these commands run, on "
    "every page: open, reload, back, forward, snapshot (or snapshot -i), get title, get url, click, fill, select, "
    "press (only the keys browser_input lists), scroll, scrollintoview, hover, wait (a time, an element or --load "
    "networkidle), screenshot (or screenshot --full; not on the page the login was filled on) and tab (but not "
    "tab new or tab close). Scripts, reading field values, network routes and every other command are refused. "
    "If the task needs a command that isn't on this list, tell the person in chat what you need it for.")
# Saved-login fills by thread: (session, filled tab's main-frame loader id). Cleared when its browser closes.
_saved_fills: dict[str, set[tuple[str | None, str | None]]] = {}


def mark_saved_fill(thread_id: str, session: str | None, document: str | None) -> None:
    """Before a saved login is typed: allowlist its document until a new one commits; limit its session to the end."""
    _saved_fills.setdefault(thread_id, set()).add((session, document or None))


def _same_session(marked: str | None, session: str | None) -> bool:
    return marked is None or session is None or marked == session  # a missing id can't be told from the filled session


def saved_fill_session(thread_id: str, session: str | None) -> bool:
    """Whether a saved login was filled in this session, which then runs only _saved_command shapes until it ends."""
    return any(_same_session(marked, session) for marked, _ in _saved_fills.get(thread_id, ()))


def saved_fill_document(thread_id: str, session: str | None, visit: str) -> bool:
    """Whether this document is one a saved login was filled in; an id that wasn't read, on either side, counts."""
    return any(_same_session(marked, session) and (document is None or not visit or document == visit)
               for marked, document in _saved_fills.get(thread_id, ()))


def drop_thread(thread_id: str) -> None:
    """Forget a thread's fills when its browser closes."""
    _saved_fills.pop(thread_id, None)
