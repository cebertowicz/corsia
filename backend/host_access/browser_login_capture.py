"""Hand the login the person submits themselves in the shared browser to the saved-login offer holder.

Only the person's own submit counts: a pane mousePressed, or a pane Enter, read just before it reaches the page.
With a password step open, the step's visible password field and a username beside it are read in an isolated
world and offered (`offer_login`); with none open, a filled-in username is noted (`note_username`), for a sign-in
that asks for the password on its next page. The step's ending then confirms or drops the offer.

What was typed stays in local variables between the read and the holder's call: no log line, exception text,
step, event, global or attribute here ever holds it. Everything kept here is non-secret: which session has an
offer waiting and its site's domain, and which documents an agent may have typed into.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import shlex
from dataclasses import dataclass
from typing import Mapping, Sequence
from urllib.parse import urlsplit

import websockets
from websockets.exceptions import WebSocketException

from host_access import browser_cdp_frames
from host_access.browser_cdp import CdpClient, PasswordProbe, border_rect, protocol_int
from host_access.browser_domain import registrable_domain
from host_access.browser_session_state import BrowserSessionError
from infra import saved_login_offers

log = logging.getLogger(__name__)
# A read that takes longer than this is dropped, so the person's click or Enter is never held up for long.
CAPTURE_TIMEOUT = 1.5
_WORLD = "corsia-login-capture"
# Agent commands that can't put a value into a field (Governance rule 7); every other command marks the document.
_NO_VALUE_COMMANDS = frozenset({
    "click", "dblclick", "hover", "focus", "scroll", "scrollintoview", "get", "is", "snapshot", "screenshot",
    "wait", "tab", "open", "goto", "navigate", "back", "forward", "reload",
})
# Stands for "a document whose loader couldn't be read": the thread's every document counts as marked.
_ANY_DOCUMENT = ""
_MARKS_KEPT = 64

# The isolated-world read of a password field and a username beside it, as [typed, username]; null when the node
# isn't an input of this world's document. Username: an autocomplete~=username input in the field's form (or the
# document), else a type=email one, else the last visible text, email or tel input before the field.
_READ_LOGIN = """function() {
  if (!(this instanceof HTMLInputElement) || this.ownerDocument !== document) return null;
  const field = this;
  const inputs = Array.from((field.form || document).querySelectorAll('input')).filter(el => el !== field);
  const tokens = el => (el.getAttribute('autocomplete') || '').toLowerCase().split(/\\s+/);
  const shows = el => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const before = inputs.filter(el => ['text', 'email', 'tel'].includes(el.type) && shows(el)
    && (el.compareDocumentPosition(field) & Node.DOCUMENT_POSITION_FOLLOWING));
  const account = inputs.find(el => tokens(el).includes('username'))
    || inputs.find(el => el.type === 'email') || before[before.length - 1];
  return [field.value, account ? account.value : ''];
}"""
# The isolated-world read of a username the person filled in: the focused input, else one in the focused
# element's form, that is type=email or autocomplete~=username/email and not empty; null otherwise.
_READ_ACCOUNT = """(() => {
  const tokens = el => (el.getAttribute('autocomplete') || '').toLowerCase().split(/\\s+/);
  const named = el => el instanceof HTMLInputElement && el.value !== ''
    && (el.type === 'email' || tokens(el).includes('username') || tokens(el).includes('email'));
  const active = document.activeElement;
  if (named(active)) return active.value;
  const found = active && active.form ? Array.from(active.form.elements).find(named) : undefined;
  return found ? found.value : null;
})()"""


class _Silent(logging.Logger):
    """A logger that is never enabled, so the websocket carrying a read logs none of its frames at any level."""

    def isEnabledFor(self, level: int) -> bool:  # noqa: N802 - logging.Logger's own name
        return False


class _QuietCdp(CdpClient):
    """CdpClient whose socket never logs: at DEBUG, websockets logs the start of every frame it receives."""

    async def __aenter__(self) -> _QuietCdp:
        self.socket = await websockets.connect(self.endpoint, max_size=2**20, open_timeout=3, close_timeout=1,
                                               logger=_Silent(_WORLD))
        return self


@dataclass(frozen=True)
class _Pending:
    """An offer waiting on its step's ending: never the login itself."""

    session_key: str
    domain: str


_pending: dict[str, _Pending] = {}
# Each thread's documents (main-frame loader ids) an agent command may have typed into, oldest first.
_marks: dict[str, dict[str, None]] = {}


def is_submit(kind: object, event: Mapping[str, object]) -> bool:
    """Whether a pane input is the person's own submit: a mouse press, or an Enter key press (rule 1)."""
    if kind == "input_mouse":
        return event.get("eventType") == "mousePressed"
    return kind == "input_keyboard" and event.get("eventType") in {"keyDown", "rawKeyDown"} and event.get("key") == "Enter"


def writes_value(args: Sequence[str]) -> bool:
    """Whether an agent command can put a value into a field (rule 7); a batch counts if any command in it can."""
    if not args:
        return False
    if args[0] == "batch":
        try:
            nested = [shlex.split(arg) for arg in args[1:] if arg != "--bail"]
        except ValueError:
            return True
        return any(writes_value(command) for command in nested)
    # wait --fn runs a page script, which can set a field.
    return args[0] not in _NO_VALUE_COMMANDS or args[0] == "wait" and "--fn" in args


async def agent_writing(thread_id: str, endpoint: str) -> None:
    """Mark the page's current document before an agent command that can type into it runs (rule 7)."""
    loader = _ANY_DOCUMENT
    try:
        if endpoint:
            async with _QuietCdp(endpoint) as cdp:
                loader, _ = await _frame_tree(cdp)
    except (BrowserSessionError, OSError, WebSocketException):
        log.info("saved-login capture couldn't read the page an agent typed into; no offer is made in this session")
    marks = _marks.setdefault(thread_id, {})
    marks[loader] = None
    while len(marks) > _MARKS_KEPT:
        del marks[next(iter(marks))]


def _marked(thread_id: str, loader: str) -> bool:
    marks = _marks.get(thread_id, {})
    return not loader or loader in marks or _ANY_DOCUMENT in marks


async def person_submit(thread_id: str, session_key: str, endpoint: str) -> None:
    """Read and hand over what the person is submitting; never raises and never holds input past the timeout."""
    try:
        await asyncio.wait_for(_submit(thread_id, session_key, endpoint), CAPTURE_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception:
        # Fixed text only: an exception here may carry page data.
        log.warning("saved-login capture skipped a submit it couldn't read")


async def _submit(thread_id: str, session_key: str, endpoint: str) -> None:
    # Imported at call time: the step lifecycle imports this module for its endings.
    lifecycle = importlib.import_module("host_access.browser_step_lifecycle")
    step = lifecycle.password_step(thread_id)
    async with _QuietCdp(endpoint) as cdp:
        loader, frames = await _frame_tree(cdp)
        if _marked(thread_id, loader):
            return
        if step is None:
            account = await _read_account(cdp, frames)
            if account is not None:
                saved_login_offers.note_username(session_key, account[0], account[1])
            return
        login = await _read_login(cdp, step.nodes, frames)
    # The step may have ended while the page was read; an offer only goes to a step still open.
    if login is None or lifecycle.password_step(thread_id) is not step:
        return
    # offer_login reads the database, so it runs off the event loop.
    if not await asyncio.to_thread(saved_login_offers.offer_login, session_key, login[0], login[1], login[2]):
        return
    # The step may also have ended while the holder checked the database: its ending never saw this offer.
    if lifecycle.password_step(thread_id) is step:
        _pending[thread_id] = _Pending(session_key, _site_domain(login[0]))
    else:
        _settle(session_key, confirm=False)


async def _frame_tree(cdp: CdpClient) -> tuple[str, list[tuple[str, str]]]:
    """The main frame's loader id, and each frame of the page target's tree with its security origin."""
    tree = (await cdp.call("Page.getFrameTree")).get("frameTree") or {}
    loader = (tree.get("frame") or {}).get("loaderId")
    frames: list[tuple[str, str]] = []
    pending = [tree]
    while pending and len(frames) < browser_cdp_frames.FRAME_LIMIT:
        node = pending.pop(0)
        frame = node.get("frame") or {}
        if isinstance(frame.get("id"), str):
            frames.append((frame["id"], str(frame.get("securityOrigin") or "")))
        pending.extend(child for child in node.get("childFrames") or [] if isinstance(child, dict))
    return (loader if isinstance(loader, str) else ""), frames


async def _read_login(cdp: CdpClient, nodes: frozenset[int],
                      frames: list[tuple[str, str]]) -> tuple[str, str, str] | None:
    """(site, username, typed) from the step's visible fields in the top document or an in-process frame.

    A field in an out-of-process frame is skipped (no capture in v1), and so is a frame that isn't https.
    Two filled fields holding different values (a change-password form) make no offer.
    """
    worlds: dict[str, int | None] = {}
    found: list[tuple[str, str, str]] = []
    for key in sorted(nodes):
        document, node = browser_cdp_frames.split_key(key)
        rect = await border_rect(cdp, backendNodeId=node) if document is None else None
        if rect is None or rect["width"] <= 0 or rect["height"] <= 0:
            continue
        for frame_id, origin in frames:
            if not _secure_site(origin):
                continue
            if frame_id not in worlds:
                worlds[frame_id] = await _world(cdp, frame_id)
            read = await _call_on(cdp, node, worlds[frame_id])
            if read is not None:
                if read[0]:
                    found.append((origin, read[1], read[0]))
                break
    if len({entry[2] for entry in found}) != 1:
        return None
    return found[0]


async def _read_account(cdp: CdpClient, frames: list[tuple[str, str]]) -> tuple[str, str] | None:
    """(site, username) the person filled in on a page with no password step, from the first frame holding one."""
    for frame_id, origin in frames:
        if not _secure_site(origin) or (context := await _world(cdp, frame_id)) is None:
            continue
        try:
            outcome = await cdp.call("Runtime.evaluate", expression=_READ_ACCOUNT, contextId=context,
                                     returnByValue=True, silent=True)
        except BrowserSessionError:
            continue
        value = (outcome.get("result") or {}).get("value") if "exceptionDetails" not in outcome else None
        if isinstance(value, str) and value:
            return origin, value
    return None


async def _world(cdp: CdpClient, frame_id: str) -> int | None:
    """A fresh isolated world in the frame, so no page script sees the read; None for a frame this target can't reach."""
    try:
        world = await cdp.call("Page.createIsolatedWorld", frameId=frame_id, worldName=_WORLD, grantUniveralAccess=False)
        return protocol_int(world.get("executionContextId"))
    except BrowserSessionError:
        return None


async def _call_on(cdp: CdpClient, node: int, context: int | None) -> tuple[str, str] | None:
    """Run _READ_LOGIN on the node in the isolated world `context`; None when the node isn't in its document."""
    if context is None:
        return None
    try:
        resolved = await cdp.call("DOM.resolveNode", backendNodeId=node, executionContextId=context)
        object_id = (resolved.get("object") or {}).get("objectId")
        if not isinstance(object_id, str):
            return None
        outcome = await cdp.call("Runtime.callFunctionOn", objectId=object_id, functionDeclaration=_READ_LOGIN,
                                 returnByValue=True, silent=True)
    except BrowserSessionError:
        return None
    value = (outcome.get("result") or {}).get("value") if "exceptionDetails" not in outcome else None
    if isinstance(value, list) and len(value) == 2 and all(isinstance(item, str) for item in value):
        return value[0], value[1]
    return None


def _secure_site(origin: str) -> bool:
    """Only an https frame's own security origin is a site (rule 3)."""
    return urlsplit(origin).scheme == "https" and bool(urlsplit(origin).hostname)


def _site_domain(site: str) -> str:
    return registrable_domain(urlsplit(site).hostname or "")


def step_decided(thread_id: str, result: PasswordProbe | None, end: str | None, opened: str | None) -> None:
    """Settle a waiting offer from one step event (rules 5 and 6); never raises into the lifecycle.

    A probe of another registrable domain drops it. Otherwise the step's ending confirms it only when the step
    settled, this event's page shows no password field, and no new step opened; any other ending drops it.
    """
    try:
        if (pending := _pending.get(thread_id)) is None:
            return
        if result is not None and registrable_domain(result.host) != pending.domain:
            _resolve(thread_id, confirm=False)
        elif end is not None:
            _resolve(thread_id, confirm=end == "settle" and result is not None and not result.visible_nodes
                     and opened is None)
    except Exception:
        log.warning("saved-login capture couldn't settle an offer after a browser step event")


def session_ended(thread_id: str) -> None:
    """The browser session ended: drop any waiting offer and forget the agent's marks.

    The holder's own drop when the browser closes (`browser_control.browser_closed` calling
    `saved_login_offers.drop_thread`) is the safety net; this clears what capture keeps.
    """
    _resolve(thread_id, confirm=False)
    _marks.pop(thread_id, None)


def _resolve(thread_id: str, *, confirm: bool) -> None:
    if (pending := _pending.pop(thread_id, None)) is not None:
        _settle(pending.session_key, confirm=confirm)


def _settle(session_key: str, *, confirm: bool) -> None:
    try:
        if confirm:
            saved_login_offers.confirm_offer(session_key)
        else:
            saved_login_offers.drop_offer(session_key)
    except Exception:
        log.warning("saved-login capture couldn't %s an offer", "confirm" if confirm else "drop")

