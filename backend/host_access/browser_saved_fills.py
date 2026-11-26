"""Which documents and sessions a saved login or item was filled in; browser_control applies the limits they carry.

A screenshot in such a session is also checked here first (refuse_screenshot_showing_value): the page's snapshot,
field values included, is read through the same command path, and the screenshot is refused if any saved value
shows in it in any form browser_output_blanking blanks, or if the saved values can't be read. Text drawn in an
image, a canvas or CSS generated content isn't in a snapshot, so it isn't seen.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Sequence

from host_access import browser_cli, browser_output_blanking
from host_access.browser_session_state import COMMAND_TIMEOUT, BrowserSessionError

log = logging.getLogger(__name__)
# After a saved fill, the session runs only browser_session_commands._saved_command shapes until it ends.
SAVED_FILL_SESSION_REFUSED = (
    "A saved login or saved item was filled in this browser session, so until the session ends only these commands "
    "run, on every page: open, reload, back, forward, snapshot (or snapshot -i), get title, get url, click, fill, "
    "select, press (only the keys browser_input lists), scroll, scrollintoview, hover, wait (a time, an element or "
    "--load networkidle), screenshot (or screenshot --full; not on the page it was filled on, nor on a page that "
    "shows a saved value) and tab (but not tab new or tab close). Scripts, reading field values, network routes and "
    "every other command are refused. "
    "If the task needs a command that isn't on this list, tell the person in chat what you need it for.")
SCREENSHOT_SHOWS_SAVED_VALUE = (
    "This screenshot was refused: this page shows a saved login's or saved item's value, or couldn't be checked for "
    "one. Take a snapshot instead; it shows saved values hidden.")
# Runs one browser command in the session (browser_session_commands._command) and returns its result.
Command = Callable[[Sequence[str], float], Awaitable[browser_cli.CommandResult]]
# Saved fills (login or item) by thread: (session, filled tab's main-frame loader id). Cleared when its browser closes.
_saved_fills: dict[str, set[tuple[str | None, str | None]]] = {}


def mark_saved_fill(thread_id: str, session: str | None, document: str | None) -> None:
    """Before a saved login or item is filled: allowlist its document until a new one commits; limit its session."""
    _saved_fills.setdefault(thread_id, set()).add((session, document or None))


def same_session(marked: str | None, session: str | None) -> bool:
    return marked is None or session is None or marked == session  # a missing id can't be told from the filled session


def saved_fill_session(thread_id: str, session: str | None) -> bool:
    """Whether a saved login or item was filled in this session, which then runs only _saved_command shapes."""
    return any(same_session(marked, session) for marked, _ in _saved_fills.get(thread_id, ()))


def saved_fill_document(thread_id: str, session: str | None, visit: str) -> bool:
    """Whether a saved login or item was filled in this document; an id that wasn't read, on either side, counts."""
    return any(same_session(marked, session) and (document is None or not visit or document == visit)
               for marked, document in _saved_fills.get(thread_id, ()))


async def refuse_screenshot_showing_value(thread_id: str, session: str | None, args: Sequence[str], command: Command,
                                         timeout: float) -> None:
    """In a saved-fill session, refuse a screenshot of a page whose snapshot shows a saved value, or can't be read.

    The snapshot is the agent-browser one, field values included, read through the session's own command path and
    never returned; only whether a saved value shows in it is kept.
    """
    if not args or args[0] != "screenshot" or not saved_fill_session(thread_id, session):
        return
    try:
        result = await command(("snapshot",), min(timeout, COMMAND_TIMEOUT))
        unreadable = result.timed_out or result.completion == "interrupted" or result.returncode != 0
        shows = unreadable or await asyncio.to_thread(
            browser_output_blanking.shows_saved_value, f"{result.stdout}\n{result.stderr}")
    except Exception as exc:  # noqa: BLE001 - a page or store that can't be checked refuses; the type only is logged
        log.info("screenshot check for thread %s failed: %s", thread_id, type(exc).__name__)
        shows = True
    if shows:
        raise BrowserSessionError(SCREENSHOT_SHOWS_SAVED_VALUE)


def drop_thread(thread_id: str) -> None:
    """Forget a thread's fills when its browser closes."""
    _saved_fills.pop(thread_id, None)
