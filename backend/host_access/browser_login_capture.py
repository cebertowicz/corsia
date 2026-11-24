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
from dataclasses import dataclass, field
from typing import Mapping, Sequence
from urllib.parse import urlsplit

from websockets.exceptions import WebSocketException

from host_access import browser_cdp_frames
from host_access.browser_cdp import CdpClient, PasswordProbe, border_rect
from host_access.browser_login_capture_cdp import call_on as _call_on, frame_tree as _frame_tree, world as _world
from host_access.browser_login_capture_queue import QueueItem as _QueueItem, enqueue as _queue
from host_access.browser_login_capture_queue import items as _items, tails as _tails, tasks as _tasks
from host_access.browser_domain import registrable_domain
from host_access.browser_session_state import BrowserSessionError
from infra import saved_login_offers

log = logging.getLogger(__name__)
# Real Chrome measured max 6.87 ms; the bound only stops a stuck page holding the click.
CAPTURE_TIMEOUT = 1.5
_WORLD = "corsia-login-capture"
# Agent commands that can't put a value into a field (Governance rule 7); every other command marks the document.
_NO_VALUE_COMMANDS = frozenset({
    "click", "dblclick", "hover", "focus", "scroll", "scrollintoview", "get", "is", "snapshot", "screenshot",
    "wait", "tab", "open", "goto", "navigate", "back", "forward", "reload",
})
# Stands for "a document whose loader couldn't be read": the thread's every document counts as marked.
_ANY_DOCUMENT = ""

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


def _QuietCdp(endpoint: str) -> CdpClient:  # noqa: N802 - retained as the capture test seam
    """Use the common CDP client with frame logging disabled for this read."""
    return CdpClient(endpoint, logger=_Silent(_WORLD))


@dataclass(frozen=True)
class _Pending:
    """An offer waiting on its step's ending: never the login itself."""

    session_key: str
    domain: str


@dataclass
class _StepSubmits:
    step: object
    session_key: str
    offered: bool = False
    number: int = 0


@dataclass
class _Flight:
    group: _StepSubmits
    number: int
    session_key: str
    domain: str | None = None
    verdict: bool | None = None
    probe_domains: set[str] = field(default_factory=set)


_pending: dict[str, _Pending] = {}
_flights: dict[str, list[_Flight]] = {}
_groups: dict[str, _StepSubmits] = {}
# Each thread's documents (main-frame loader ids) an agent command may have typed into.
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


def _marked(thread_id: str, loader: str) -> bool:
    marks = _marks.get(thread_id, {})
    return not loader or loader in marks or _ANY_DOCUMENT in marks


async def person_submit(thread_id: str, session_key: str, endpoint: str) -> None:
    """Read before delivering input, then queue holder work without holding the input."""
    lifecycle = importlib.import_module("host_access.browser_step_lifecycle")
    step = lifecycle.password_step(thread_id)
    item = _QueueItem(session_key)
    _items.setdefault(thread_id, []).append(item)
    previous = _tails.get(thread_id)
    finished = asyncio.get_running_loop().create_future()
    _tails[thread_id] = finished
    flight = _start_submit(thread_id, session_key, step) if step is not None else None
    read = None
    try:
        read = await asyncio.wait_for(_submit(thread_id, endpoint, step), CAPTURE_TIMEOUT)
    except asyncio.CancelledError:
        _queue(thread_id, item, previous, finished, _discard(thread_id, flight),
               lambda: _forget(thread_id, flight) if flight is not None else None)
        raise
    except Exception:
        log.warning("saved-login capture skipped a submit it couldn't read")
    if flight is not None:
        if read is None or lifecycle.password_step(thread_id) is not step:
            work = _discard(thread_id, flight)
        else:
            flight.domain = _site_domain(read[0])
            if any(domain != flight.domain for domain in flight.probe_domains):
                flight.verdict = False
            work = _offer(thread_id, item, flight, read)
    else:
        work = _note(session_key, read)
    _queue(thread_id, item, previous, finished, work,
           lambda: _forget(thread_id, flight) if flight is not None else None)


def _start_submit(thread_id: str, session_key: str, step: object) -> _Flight:
    group = _groups.get(thread_id)
    if group is None or group.step is not step or group.session_key != session_key:
        group = _StepSubmits(step, session_key)
        _groups[thread_id] = group
    else:
        for earlier in _flights.get(thread_id, []):
            if earlier.group is group:
                earlier.verdict = None
    group.number += 1
    flight = _Flight(group, group.number, session_key)
    _flights.setdefault(thread_id, []).append(flight)
    _pending.pop(thread_id, None)
    return flight


async def _submit(thread_id: str, endpoint: str, step):
    async with _QuietCdp(endpoint) as cdp:
        loader, frames = await _frame_tree(cdp)
        if _marked(thread_id, loader):
            return None
        if step is None:
            return await _read_account(cdp, frames)
        return await _read_login(cdp, step.nodes, frames)


async def _note(session_key, account) -> None:
    if account is not None:
        try:
            saved_login_offers.note_username(session_key, account[0], account[1])
        except Exception:
            log.warning("saved-login capture couldn't note an account")


async def _discard(thread_id: str, flight: _Flight | None) -> None:
    if flight is not None:
        if flight.group.offered:
            _settle(flight.session_key, confirm=False)
            flight.group.offered = False
        _forget(thread_id, flight)


def _forget(thread_id: str, flight: _Flight) -> None:
    flights = _flights.get(thread_id, [])
    if flight in flights:
        flights.remove(flight)
    if not flights:
        _flights.pop(thread_id, None)


async def _offer(thread_id: str, item: _QueueItem, flight: _Flight, login: tuple[str, str, str]) -> None:
    if flight.verdict is False:
        await _discard(thread_id, flight)
        return
    try:
        made = await asyncio.to_thread(saved_login_offers.offer_login, flight.session_key, login[0], login[1], login[2])
    except Exception:
        log.warning("saved-login capture skipped a submit it couldn't read")
        made = False
    flight.group.offered = made
    _forget(thread_id, flight)
    if not made:
        return
    if item.ended:
        _settle(item.session_key, confirm=False)
        flight.group.offered = False
        return
    if flight.verdict is False:
        _settle(flight.session_key, confirm=False)
        flight.group.offered = False
        return
    if flight.number != flight.group.number:
        return
    lifecycle = importlib.import_module("host_access.browser_step_lifecycle")
    if flight.verdict is not None:
        _settle(flight.session_key, confirm=flight.verdict)
        flight.group.offered = False
    elif lifecycle.password_step(thread_id) is not flight.group.step:
        _settle(flight.session_key, confirm=False)
        flight.group.offered = False
    else:
        _pending[thread_id] = _Pending(flight.session_key, flight.domain or "")


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
                worlds[frame_id] = await _world(cdp, frame_id, _WORLD)
            read = await _call_on(cdp, node, worlds[frame_id], _READ_LOGIN)
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
        if not _secure_site(origin) or (context := await _world(cdp, frame_id, _WORLD)) is None:
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
        group = _groups.get(thread_id)
        flight = next((item for item in reversed(_flights.get(thread_id, []))
                       if item.group is group and item.number == group.number), None) if group else None
        if flight is not None:
            if result is not None:
                domain = registrable_domain(result.host)
                flight.probe_domains.add(domain)
                if flight.domain is not None and flight.domain != domain:
                    flight.verdict = False
            if end is not None and flight.verdict is not False:
                flight.verdict = (end == "settle" and result is not None and not result.visible_nodes
                                  and opened is None)
            return
        if (pending := _pending.get(thread_id)) is None:
            return
        if result is not None and registrable_domain(result.host) != pending.domain:
            _resolve(thread_id, confirm=False)
        elif end is not None:
            _resolve(thread_id, confirm=end == "settle" and result is not None and not result.visible_nodes
                     and opened is None)
    except Exception:
        log.warning("saved-login capture couldn't settle an offer after a browser step event")


def session_ended(thread_id: str, session_key: str | None = None) -> None:
    """The browser session ended: drop any waiting offer and forget the agent's marks.

    The holder's own drop when the browser closes (`browser_control.browser_closed` calling
    `saved_login_offers.drop_thread`) is the safety net; this clears what capture keeps.
    """
    group = _groups.get(thread_id)
    if session_key is None:
        pending = _pending.get(thread_id)
        session_key = group.session_key if group is not None else next(
            (item.session_key for item in reversed(_items.get(thread_id, []))),
            pending.session_key if pending is not None else None)
    for item in _items.get(thread_id, []):
        if item.session_key == session_key:
            item.ended = True
    if group is not None and group.session_key == session_key:
        _groups.pop(thread_id, None)
    if (pending := _pending.get(thread_id)) is not None and pending.session_key == session_key:
        _resolve(thread_id, confirm=False)
    _marks.pop(thread_id, None)


def _resolve(thread_id: str, *, confirm: bool) -> None:
    if (pending := _pending.pop(thread_id, None)) is not None:
        _settle(pending.session_key, confirm=confirm)
        if (group := _groups.get(thread_id)) is not None and group.session_key == pending.session_key:
            group.offered = False


def _settle(session_key: str, *, confirm: bool) -> None:
    try:
        if confirm:
            saved_login_offers.confirm_offer(session_key)
        else:
            saved_login_offers.drop_offer(session_key)
    except Exception:
        log.warning("saved-login capture couldn't %s an offer", "confirm" if confirm else "drop")

