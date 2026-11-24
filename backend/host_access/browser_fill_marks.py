"""Which documents an agent ran a script in, and which sessions it changed past a document, for the saved-login fill.

A saved login is filled only on a clean document: one where no agent eval or script ran since it loaded and
no agent network route is active (browser_login_fill). Before an agent eval or `wait --fn`, or a no-script
command given an option outside _PLAIN_OPTIONS, the page's current document, its main frame's loader id, is
marked; a reload or navigation commits a new loader, which starts clean. A document whose loader can't be read
marks every document of the thread. Any other command outside _NO_SCRIPT_COMMANDS marks the whole session: a
network route, init script or header outlives a reload, and an undo isn't trusted to have removed it. Both
are kept until the session ends (forget); past _MARKS_KEPT documents every document counts as marked. Only
identities are kept here, never page content.
"""
from __future__ import annotations

import logging
import shlex
from typing import Sequence

from websockets.exceptions import WebSocketException

from host_access.browser_cdp import CdpClient
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
# Stands for "a document whose loader couldn't be read": every document of the thread counts as marked.
_ANY_DOCUMENT = ""
_MARKS_KEPT = 64

_scripted: dict[str, dict[str, None]] = {}
_session_marks: set[str] = set()


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


async def before_agent_command(thread_id: str, session: str | None, endpoint: str, args: Sequence[str]) -> None:
    """Mark the session before a command that outlives its document, and the page's document before a script."""
    if outlives_document(args):
        _session_marks.add(f"{thread_id}:{session}")
    if runs_script(args):
        _mark(thread_id, await _loader(endpoint))


async def _loader(endpoint: str) -> str:
    try:
        if endpoint:
            async with CdpClient(endpoint) as cdp:
                return main_loader(await cdp.call("Page.getFrameTree"))
    except (BrowserSessionError, OSError, WebSocketException):
        log.info("an agent's script ran on a page whose document couldn't be read; no saved login is filled there")
    return _ANY_DOCUMENT


def main_loader(tree: dict) -> str:
    """The loader id of a frame tree's main frame, or the any-document mark when it has none."""
    loader = ((tree.get("frameTree") or {}).get("frame") or {}).get("loaderId")
    return loader if isinstance(loader, str) and loader else _ANY_DOCUMENT


def _mark(thread_id: str, loader: str) -> None:
    marks = _scripted.setdefault(thread_id, {})
    marks[loader] = None
    if len(marks) > _MARKS_KEPT:
        # Forgetting an old mark could make a live document look clean, so past the cap every document counts.
        _scripted[thread_id] = {_ANY_DOCUMENT: None}


def scripted(thread_id: str, loader: str) -> bool:
    """Whether an agent may have run a script in this document since it loaded."""
    marks = _scripted.get(thread_id, {})
    return not loader or loader in marks or _ANY_DOCUMENT in marks


def session_marked(thread_id: str, session: str | None) -> bool:
    """Whether an agent may have changed this session past a document, a network route included."""
    # A command or a fill with no session id can't be told apart from any session, so it counts as marked.
    return session is None or f"{thread_id}:{session}" in _session_marks or f"{thread_id}:None" in _session_marks


def forget(thread_id: str) -> None:
    """The thread's browser closed: its documents, routes and scripts are gone with it."""
    _scripted.pop(thread_id, None)
    for key in [key for key in _session_marks if key.startswith(f"{thread_id}:")]:
        _session_marks.discard(key)
