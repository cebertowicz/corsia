"""Hand the login the person submits themselves in the shared browser to the saved-login offer holder.

Only the person's own submit counts: a pane mousePressed, or a pane Enter, read just before it reaches the page.
With a password step open, the step's visible password field and a username beside it are read in an isolated
world and offered (`offer_login`); with none open, a filled-in username is noted (`note_username`), for a sign-in
that asks for the password on its next page. The step's ending then confirms or drops the offer.

What was typed stays in local variables between the read and the holder's call: no log line, exception text,
step, event, global or attribute here ever holds it. Everything kept here is non-secret: which session has an
offer waiting and its site's domain, and which documents an agent may have typed into.

Offer transitions (flight and pending are exclusive: flight is forgotten before pending is set):
    submit -> flight; read failed/timeout -> none after queued drop; read done -> holder call
    offer made -> waiting, refused -> none; probe of another domain -> dropped
    step settles without a field -> confirmed; other step end -> dropped
    new step/session -> old offer dropped in queue order; session end -> dropped or skipped
Confirmed and dropped offers leave capture's pending state; the holder owns confirmed offers.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from websockets.exceptions import WebSocketException

from host_access import browser_cdp_frames
from host_access.browser_cdp import CdpClient, PasswordProbe, border_rect
from host_access.browser_login_capture_cdp import call_on as _call_on, frame_tree as _frame_tree, world as _world
from host_access.browser_login_capture_cdp import quiet_cdp as _QuietCdp
from host_access import browser_login_capture_queue as _queue_state
from host_access.browser_login_capture_policy import is_submit, writes_value
from host_access.browser_login_capture_queue import QueueItem as _QueueItem, enqueue as _queue
from host_access.browser_login_capture_queue import tasks as _tasks
from host_access.browser_domain import registrable_domain
from host_access.browser_session_state import BrowserSessionError
from infra import saved_login_offers

log = logging.getLogger(__name__)
# Measured on PR #3242 (figures in its body); one second bounds a delay that can interrupt a person's thought
# (Nielsen, "Response Times: The 3 Important Limits"). A timeout skips capture; the click still goes through.
CAPTURE_TIMEOUT = 1.0
_WORLD = "corsia-login-capture"
# Capture marks every agent command that can put a value into a field.
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
    replaced_group: _StepSubmits | None = None


@dataclass
class _ThreadCapture:
    group: _StepSubmits | None = None
    flights: list[_Flight] = field(default_factory=list)
    pending: _Pending | None = None
    marks: dict[str, None] = field(default_factory=dict)


_threads: dict[str, _ThreadCapture] = {}


def _state(thread_id: str) -> _ThreadCapture:
    return _threads.setdefault(thread_id, _ThreadCapture())


def _reset_for_tests() -> None:
    _threads.clear()
    _queue_state.reset_for_tests()


def _prune(thread_id: str) -> None:
    state = _threads.get(thread_id)
    if state is not None and not (state.group or state.flights or state.pending or state.marks):
        _threads.pop(thread_id, None)


async def agent_writing(thread_id: str, endpoint: str) -> None:
    """Mark the page's current document before an agent command that can type into it runs (rule 7)."""
    loader = _ANY_DOCUMENT
    try:
        if endpoint:
            async with _QuietCdp(endpoint) as cdp:
                loader, _ = await _frame_tree(cdp)
    except (BrowserSessionError, OSError, WebSocketException):
        log.info("saved-login capture couldn't read the page an agent typed into; no offer is made in this session")
    marks = _state(thread_id).marks
    marks[loader] = None


def _marked(thread_id: str, loader: str) -> bool:
    state = _threads.get(thread_id)
    marks = state.marks if state is not None else {}
    return not loader or loader in marks or _ANY_DOCUMENT in marks


async def person_submit(thread_id: str, session_key: str, endpoint: str) -> None:
    """Read before delivering input, then queue holder work without holding the input."""
    lifecycle = importlib.import_module("host_access.browser_step_lifecycle")
    step = lifecycle.password_step(thread_id)
    item, previous, finished = _queue_state.begin(thread_id, session_key)
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
    state = _state(thread_id)
    group = state.group
    replaced = None
    if group is None or group.step is not step or group.session_key != session_key:
        replaced = group
        group = _StepSubmits(step, session_key)
        state.group = group
    else:
        for earlier in state.flights:
            if earlier.group is group:
                earlier.verdict = None
    group.number += 1
    flight = _Flight(group, group.number, session_key, replaced_group=replaced)
    state.flights.append(flight)
    state.pending = None
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
        _drop_replaced(flight)
        if flight.group.offered:
            _settle(flight.session_key, confirm=False)
            flight.group.offered = False
        _forget(thread_id, flight)


def _forget(thread_id: str, flight: _Flight) -> None:
    flights = _state(thread_id).flights
    if flight in flights:
        flights.remove(flight)
    _prune(thread_id)


def _drop_replaced(flight: _Flight) -> None:
    group = flight.replaced_group
    if group is not None and group.offered:
        _settle(group.session_key, confirm=False)
        group.offered = False
    flight.replaced_group = None


async def _offer(thread_id: str, item: _QueueItem, flight: _Flight, login: tuple[str, str, str]) -> None:
    _drop_replaced(flight)
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
    if item.ended or _state(thread_id).group is not flight.group:
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
        _state(thread_id).pending = _Pending(flight.session_key, flight.domain or "")


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
        state = _state(thread_id)
        group = state.group
        flight = next((item for item in reversed(state.flights)
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
        if (pending := state.pending) is None:
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
    state = _state(thread_id)
    group = state.group
    if session_key is None:
        pending = state.pending
        session_key = (group.session_key if group is not None else
                       _queue_state.latest_session(thread_id) or (pending.session_key if pending is not None else None))
    _queue_state.end_session(thread_id, session_key)
    if group is not None and group.session_key == session_key:
        state.group = None
    if (pending := state.pending) is not None and pending.session_key == session_key:
        _resolve(thread_id, confirm=False)
    state.marks.clear()
    _prune(thread_id)


def _resolve(thread_id: str, *, confirm: bool) -> None:
    state = _state(thread_id)
    if (pending := state.pending) is not None:
        state.pending = None
        _settle(pending.session_key, confirm=confirm)
        if (group := state.group) is not None and group.session_key == pending.session_key:
            group.offered = False


def _settle(session_key: str, *, confirm: bool) -> None:
    try:
        if confirm:
            saved_login_offers.confirm_offer(session_key)
        else:
            saved_login_offers.drop_offer(session_key)
    except Exception:
        log.warning("saved-login capture couldn't %s an offer", "confirm" if confirm else "drop")

