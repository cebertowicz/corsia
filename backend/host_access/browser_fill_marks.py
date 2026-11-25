"""Which documents, sites and sessions an agent ran a script in or changed, for the saved-login fill.

A saved login is filled only on a clean document (browser_login_fill). Before each agent command:

- A command that can change the session past its document (a network route, an init script, a header: any
  command outside _DOCUMENT_SCRIPT_COMMANDS) marks the whole session; an undo isn't trusted to have removed it.
- A batch with any step that can run script marks the whole session: an earlier step can navigate or switch
  tabs, so the document a later script runs in can't be known before the batch runs.
- Any other command that can run script (an eval, `wait --fn`, or a no-script command given an option outside
  _PLAIN_OPTIONS) marks the page's current document (its main frame's loader id) and that document's site (its
  registrable domain) in this session. A reload or navigation commits a new, clean document, but a script can
  leave stored state (a service worker, storage) that reaches every page of its site, so a fill on a marked
  site is not clean even after a reload. A page whose document or address can't be read marks the session.
  A page watch (browser_login_capture_writes.AgentWrites) follows the page while such a command runs
  (agent_command): a navigation that commits before the script runs (an open that didn't wait for its load, a
  page that redirects itself, `wait --fn` evaluating across loads) puts the script in a document the first read
  never saw. So any other document the watch sees, at its start, during the command or at its end, marks the
  session, as do a page opening, a watch that fails, and a command that didn't finish (it may still be running
  in the browser). The watch sees each main-frame navigation, back/forward cache restores included, so a
  history restore of the first document can't hide a document committed in between.

Marks are kept until the browser closes (forget), with no cap: each is one identity per agent script command,
freed with the session, and forgetting one could make a live document look clean. Only identities are kept
here, never page content.
"""
from __future__ import annotations

import logging
import shlex
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable, Sequence

from websockets.exceptions import WebSocketException

from host_access import browser_cdp
from host_access.browser_login_capture_writes import AgentWrites
from host_access.browser_saved_fills import same_session
from host_access.browser_domain import registrable_domain
from host_access.browser_session_state import BrowserSessionError

log = logging.getLogger(__name__)
# Agent commands that run no script of the agent's in the page (the capture code's list, plus value writes, which
# hand the page text but no code). Any other command, or any of these with an option outside _PLAIN_OPTIONS,
# marks the document.
_NO_SCRIPT_COMMANDS = frozenset({
    "click", "dblclick", "hover", "focus", "scroll", "scrollintoview", "get", "is", "snapshot", "screenshot",
    "wait", "tab", "open", "goto", "navigate", "back", "forward", "reload", "fill", "type", "press", "select",
    "check", "uncheck",
})
_PLAIN_OPTIONS = frozenset({"-i", "--full", "--load", "--url", "--text", "--state"})
# The commands whose script lives and dies with the document it ran in.
_DOCUMENT_SCRIPT_COMMANDS = _NO_SCRIPT_COMMANDS | {"eval"}

# How long the page watch may take to start, and to settle after the command.
WATCH_TIMEOUT = 5.0
# The page watch's class; tests stand in for it.
_watch = AgentWrites

# By thread: the documents (loader ids) an agent's script ran in. A loader id names one document: a back/forward
# cache restore makes that same document current again under the same id, so a mark on it still holds.
_scripted: dict[str, set[str]] = {}
# (thread, session) pairs, and (thread, session, registrable domain) triples, an agent marked.
_session_marks: set[tuple[str, str | None]] = set()
_site_marks: set[tuple[str, str | None, str]] = set()


def _nested(args: Sequence[str]) -> list[list[str]] | None:
    try:
        return [shlex.split(arg) for arg in args[1:] if arg != "--bail"]
    except ValueError:
        return None


def runs_script(args: Sequence[str]) -> bool:
    """Whether an agent command can run a script of its own in the page; a batch counts if any command in it can."""
    if not args:
        return False
    if args[0] == "batch":
        nested = _nested(args)
        return nested is None or any(runs_script(command) for command in nested)
    return args[0] not in _NO_SCRIPT_COMMANDS or any(
        arg.startswith("-") and arg not in _PLAIN_OPTIONS for arg in args[1:])


def outlives_document(args: Sequence[str]) -> bool:
    """Whether an agent command can change the session past its document (a route, an init script, a header)."""
    if not args:
        return False
    if args[0] == "batch":
        nested = _nested(args)
        return nested is None or any(outlives_document(command) for command in nested)
    return args[0] not in _DOCUMENT_SCRIPT_COMMANDS


@asynccontextmanager
async def agent_command(thread_id: str, session: str | None, endpoint: str, args: Sequence[str], *,
                        navigation: bool = False) -> AsyncIterator[Callable[[bool], None]]:
    """Mark before an agent command (before_agent_command), and after it check the document its script ran in.

    The body calls the yielded function with True once the command finished in the browser. A command that
    marked one document marks the session when it wasn't reported finished (it raised, timed out, or took
    another route), or when its page watch saw any other document, saw a page open, or failed.
    """
    loader = await before_agent_command(thread_id, session, endpoint, args, navigation=navigation)
    done = False

    def finished(ok: bool) -> None:
        nonlocal done
        done = ok

    if not loader:
        yield finished
        return
    seen: set[str] = set()
    watch = _watch(seen.add, WATCH_TIMEOUT)
    try:
        await watch.start(endpoint)
    except BaseException:
        _session_marks.add((thread_id, session))
        raise
    try:
        yield finished
    finally:
        try:
            await watch.settle()
        finally:
            if not done or seen != {loader}:
                _session_marks.add((thread_id, session))


async def before_agent_command(thread_id: str, session: str | None, endpoint: str, args: Sequence[str], *,
                               navigation: bool = False) -> str:
    """Mark the session, or the page's document and site, before an agent command that can reach them.

    Returns the document (loader id) marked, or "" when none was. `navigation` is browser_open's own
    navigation (browser_session_commands.run's trusted_navigation): a server-built eval that only sets the
    address, so it runs no script of the agent's and marks nothing. The same text sent by an agent has no
    navigation and marks as any eval does.
    """
    if navigation:
        return ""
    if outlives_document(args) or (bool(args) and args[0] == "batch" and runs_script(args)):
        _session_marks.add((thread_id, session))
    elif runs_script(args):
        page = await _page(endpoint)
        if page is None:
            _session_marks.add((thread_id, session))
            return ""
        loader, domain = page
        _scripted.setdefault(thread_id, set()).add(loader)
        _site_marks.add((thread_id, session, domain))
        return loader
    return ""


async def _page(endpoint: str) -> tuple[str, str] | None:
    """The page's document (loader id) and site (registrable domain), or None when either can't be read."""
    try:
        if endpoint:
            async with browser_cdp.CdpClient(endpoint) as cdp:
                tree = await cdp.call("Page.getFrameTree")
            loader, host = browser_cdp.loader_id(tree), browser_cdp.main_host(tree)
            if loader and host:
                return loader, registrable_domain(host)
    except (BrowserSessionError, OSError, WebSocketException, ValueError):
        pass
    log.info("an agent's script ran on a page whose document couldn't be read; no saved login is filled this session")
    return None


def scripted(thread_id: str, loader: str) -> bool:
    """Whether an agent may have run a script in this document since it loaded; an unread document counts."""
    return not loader or loader in _scripted.get(thread_id, ())


def session_marked(thread_id: str, session: str | None) -> bool:
    """Whether an agent may have changed this session past a document, or ran script it can't place."""
    return session is None or any(thread == thread_id and same_session(marked, session)
                                  for thread, marked in _session_marks)


def site_marked(thread_id: str, session: str | None, domain: str) -> bool:
    """Whether an agent ran script on a page of this site (registrable domain) in this session."""
    return any(thread == thread_id and same_session(marked, session) and site == domain
               for thread, marked, site in _site_marks)


def forget(thread_id: str) -> None:
    """The thread's browser closed: its documents, sites, routes and scripts are gone with it."""
    _scripted.pop(thread_id, None)
    _session_marks.difference_update({mark for mark in _session_marks if mark[0] == thread_id})
    _site_marks.difference_update({mark for mark in _site_marks if mark[0] == thread_id})
