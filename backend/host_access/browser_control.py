"""Shared-browser page guard: the person's password and person steps, and the read guard on a password visit."""
from __future__ import annotations

import asyncio
import importlib
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urlparse

from host_access import browser_pane_state
from host_access.browser_cdp import PasswordProbe
from host_access.browser_session_state import BrowserSessionError
from host_access.browser_steps import DONE_STATES, BrowserStep, PasswordStep, PersonStep
from infra import state
from infra.database import get_db
from infra.product_events import emit_product_event

log = logging.getLogger(__name__)

PASSWORD_PAGE = (
    "This page asks for a password, so it is the person's to complete. They already see a notice on the "
    "page and a task in their attention list, and your current task waits on it; end your turn. If they "
    "decline, tell them in chat what you needed the page for and stop."
)
PAGE_CHECK_FAILED = "This page couldn't be checked"
PERSON_STEP = (
    "This browser step is the person's to complete. They already see a task in their attention list, and your "
    "current task waits on it; end your turn. If they decline, tell them in chat what you needed the page for "
    "and stop."
)
PERSON_STEP_ENDED = (
    "The person already ended the step for this page, so it can't be given to them again. "
    "Tell them in chat what you needed the page for and stop."
)
# Locates an agent's ref or CSS selector on the page as a backendNodeId, or None when it can't.
Resolve = Callable[[str], Awaitable[int | None]]
# AR6: while a step is open and a pane is connected, the probe re-runs this many seconds after the
# person's last input, and at least this often.
REPROBE_AFTER_INPUT = 0.5
REPROBE_EVERY = 2.0

@dataclass
class PasswordReadGuard:
    """Only node identities retained for a document; never a plan step or page content."""

    visit: str
    seen_nodes: frozenset[int] = frozenset()


_steps: dict[str, BrowserStep] = {}
# A document stays read-guarded independently of whether any step is open, and a thread keeps one guard
# per document it has guarded: leaving a tab never lifts it, and a loader id never recurs, so a new
# document is never over-refused. Cleared when the thread's browser closes.
_guarded_visits: dict[str, dict[str, PasswordReadGuard]] = {}
_probe_locks: dict[str, asyncio.Lock] = {}


def pane_connected(thread_id: str, connection_token: str, report: Callable[[dict], None] | None = None,
                   probe: browser_pane_state.Probe | None = None) -> None:
    """Record a shared pane; one that can probe the page resumes the open step's re-probe."""
    browser_pane_state.pane_connected(
        thread_id, connection_token, report, probe,
        on_probe=lambda thread: _start_reprobe(thread, _steps[thread]) if thread in _steps else None,
    )


def pane_disconnected(thread_id: str, connection_token: str) -> None:
    """Forget a closed pane; once none can probe the page, the open step's re-probe stops."""
    browser_pane_state.pane_disconnected(
        thread_id, connection_token,
        on_last_probe=lambda thread: _stop_reprobe(_steps[thread]) if thread in _steps else None,
    )


async def require_page_access(thread_id: str, probe: browser_pane_state.Probe, args: tuple[str, ...], *,
                              resolve: Resolve | None = None) -> None:
    """Run one probe and enforce the password-page, person-step and per-visit read guards.

    ``resolve`` locates a value or attribute read's target, so a read of a hidden password input is
    refused (AR2); without it, such a read on a guarded visit is refused.
    """
    async with _probe_locks.setdefault(thread_id, asyncio.Lock()):
        result, prior = await _guarded_probe(thread_id, probe)
        if isinstance(prior, PersonStep):
            # The person's step is still on this visit: the agent's command never touches the page or settles it.
            raise BrowserSessionError(PERSON_STEP)
        if (guarded := _guarded_visits.get(thread_id, {}).get(result.visit)) is not None:
            await _refuse_password_read(guarded, args, resolve)


async def declare_person_step(thread_id: str, probe: browser_pane_state.Probe, instruction: str) -> None:
    """Give the current page to the person as a step that settles on their next page visit.

    A password page is refused exactly as any other command is. A second declaration is a no-op while
    the visit's person step is open, and is refused after the person has ended it.
    """
    instruction = str(instruction).strip()
    if not instruction:
        raise BrowserSessionError("browser person step needs an instruction")
    async with _probe_locks.setdefault(thread_id, asyncio.Lock()):
        result, prior = await _guarded_probe(thread_id, probe, declaring=True)
        if isinstance(prior, PasswordStep):
            raise BrowserSessionError(PASSWORD_PAGE + _ending(thread_id))
        if prior is not None:
            return
        step = PersonStep(result.visit, result.host, result.target, instruction=instruction)
        await _open_step(thread_id, step)


async def _guarded_probe(thread_id: str, probe: browser_pane_state.Probe, *, declaring: bool = False) -> tuple[PasswordProbe, BrowserStep | None]:
    """Probe the agent's page under its lock; return the result and the step still open on its visit.

    A new visit ends the prior step; read guards stay per visit. A person step the person has ended is
    dropped, and a visible password field opens or keeps the visit's password step and is refused.
    The probe gets every guarded visit's nodes and re-checks those of whichever visit the tab is on.
    """
    try:
        result = await probe(_known_nodes(thread_id))
    except Exception as exc:
        if isinstance(exc, BrowserSessionError) and str(exc) == PAGE_CHECK_FAILED:
            raise
        raise BrowserSessionError(PAGE_CHECK_FAILED) from exc
    prior = await _observe_step(thread_id, result, declaring=declaring)
    if isinstance(prior, PersonStep):
        return result, prior
    if result.visible_nodes:
        if prior is None:
            prior = PasswordStep(result.visit, result.host, result.target)
            await _open_step(thread_id, prior)
        prior.shown(thread_id)
        raise BrowserSessionError(PASSWORD_PAGE + _ending(thread_id))
    return result, prior


def _ending(thread_id: str) -> str:
    if browser_pane_state.has_pane(thread_id):
        return " Post nothing in chat."
    return " If you reply, use one short line; don't restate the task or say you made it."


def _guard(thread_id: str, result: PasswordProbe, *, nodes: frozenset[int] | None = None) -> None:
    """Retain password node identities independently of the step lifecycle (AR2)."""
    guards = _guarded_visits.setdefault(thread_id, {})
    guarded = guards.setdefault(result.visit, PasswordReadGuard(result.visit))
    guarded.seen_nodes = guarded.seen_nodes | (result.visible_nodes if nodes is None else nodes)


def _known_nodes(thread_id: str) -> dict[str, frozenset[int]]:
    """Each guarded visit's password node identities, for the probe to match against the live page's visit."""
    return {visit: guarded.seen_nodes for visit, guarded in _guarded_visits.get(thread_id, {}).items()}


def person_input(thread_id: str) -> None:
    """Note the person's pane input, so an open step re-probes once that input pauses (AR6)."""
    step = _steps.get(thread_id)
    if step is not None and step.reprobe is not None:
        step.last_input = asyncio.get_running_loop().time()
        step.input_seen.set()


def browser_closed(thread_id: str) -> None:
    """Stop an open step's re-probe once the thread's browser has closed; the agent is in no page."""
    browser_pane_state.agent_left(thread_id)
    _guarded_visits.pop(thread_id, None)
    if (step := _steps.get(thread_id)) is not None:
        _stop_reprobe(step)


def _start_reprobe(thread_id: str, step: BrowserStep) -> None:
    if browser_pane_state.pane_probes(thread_id) and (step.reprobe is None or step.reprobe.done()):
        step.reprobe = asyncio.create_task(_reprobe_loop(thread_id, step))


def _stop_reprobe(step: BrowserStep) -> None:
    task, step.reprobe = step.reprobe, None
    if task is not None and task is not asyncio.current_task():
        task.cancel()


async def _reprobe_loop(thread_id: str, step: BrowserStep) -> None:
    """Re-run the probe once the person's input pauses and every REPROBE_EVERY seconds, until the step ends."""
    clock = asyncio.get_running_loop().time
    while True:
        next_probe = clock() + REPROBE_EVERY
        while True:
            # Cleared before the deadline is read, so input arriving after this always moves it.
            step.input_seen.clear()
            due = next_probe if step.last_input is None else min(next_probe, step.last_input + REPROBE_AFTER_INPUT)
            try:
                await asyncio.wait_for(step.input_seen.wait(), max(0.0, due - clock()))
            except TimeoutError:
                break
        step.last_input = None
        try:
            if not await _reprobe(thread_id, step):
                return
        except Exception:
            # A failed plan write leaves the step recorded and its loop running; the next interval retries it.
            log.warning("browser-step transition for %s failed; retrying on the next re-probe", thread_id, exc_info=True)


async def _reprobe(thread_id: str, step: BrowserStep) -> bool:
    """Probe once for an open step; return whether it is still open and its loop should go on."""
    async with _probe_locks.setdefault(thread_id, asyncio.Lock()):
        probes = browser_pane_state.pane_probes(thread_id)
        current = _steps.get(thread_id)
        if current is None or current is not step or not probes:
            return False
        # A person-ended password row needs no further page inspection; the read guard stays.
        if isinstance(step, PasswordStep):
            await _observe_step(thread_id, None)
            if step.reprobe is None:
                return False
        try:
            result = await probes[0](_known_nodes(thread_id))
        except Exception as exc:
            # A page mid-navigation can fail one probe. The step stays open, as an agent command's
            # failed probe fails closed, and the next interval probes again.
            log.info("browser-step re-probe for %s failed: %s", thread_id, exc)
            return True
        if result.target != step.target:
            # The pane shows another tab than the step's. Only the agent's own next command, which probes
            # its page, or a probe of the step's own tab can end it; a popup sign-in returns to that tab.
            log.info("browser-step re-probe for %s reached another page target", thread_id)
            return True
        still_open = await _observe_step(thread_id, result)
        return still_open is not None and still_open is step and still_open.reprobe is not None


async def _open_step(thread_id: str, step: BrowserStep) -> None:
    """Publish an active step only after its per-kind receipt succeeds and its row is checked."""
    if step.node_id is not None:
        return
    step.node_id = await asyncio.to_thread(
        _plan_write, "open_user_step", thread_id, key=f"{step.event_prefix}:{step.visit}",
        title=step.title(), description=step.description(), provenance=step.provenance,
    )
    if not isinstance(step.node_id, str) or not step.node_id:
        raise RuntimeError("browser step row creation returned no node")
    ended = await asyncio.to_thread(_step_row_state, step.node_id)
    if isinstance(step, PersonStep) and ended is not None:
        raise BrowserSessionError(PERSON_STEP_ENDED)
    _steps[thread_id] = step
    if ended is None:
        _start_reprobe(thread_id, step)
        await _emit(f"{step.event_prefix}_opened", thread_id)


async def _end_step(thread_id: str, step: BrowserStep, *, settle: bool) -> None:
    if settle:
        await _settle_step(thread_id, step)
    _stop_reprobe(step)
    _steps.pop(thread_id, None)
    step.ended(thread_id)


async def _observe_step(thread_id: str, result: PasswordProbe | None, *, declaring: bool = False) -> BrowserStep | None:
    """The shared transition path for agent and own-tab pane probes, under the probe lock.

    A person step ends on a row ending or document change; a password step ends when its field disappears. An ended password row stops monitoring but keeps
    refusing commands while its field shows. No row ending can discard this document's hidden inputs.
    """
    step = _steps.get(thread_id)
    row_state = await asyncio.to_thread(_step_row_state, step.node_id) if step is not None else None
    ends = step is not None and result is not None and (
        result.visit != step.visit
        or isinstance(step, PersonStep) and row_state is not None
        or isinstance(step, PasswordStep) and not declaring and not result.visible_nodes
    )
    # A handoff never converts an open password step; its ordinary command or pane probe settles it first.
    # A person step that ends on the same visit keeps that document's hidden inputs guarded; one that ends
    # on a new visit guards nothing there, since no password was shown on that document.
    same_visit_end = isinstance(step, PersonStep) and ends and result is not None and result.visit == step.visit
    if result is not None and (result.visible_nodes or same_visit_end and result.present_nodes):
        _guard(thread_id, result, nodes=result.present_nodes | result.visible_nodes)
    if step is None:
        return None
    if row_state is not None:
        if row_state not in DONE_STATES and not step.declined:
            step.declined = True
            await _emit(f"{step.event_prefix}_declined", thread_id)
        _stop_reprobe(step)
    if ends:
        await _end_step(thread_id, step, settle=row_state is None)
        return None
    return step


def _step_row_state(node_id: str) -> str | None:
    """The row's ended state, or None while it is still open."""
    public = importlib.import_module("plans.public")
    db = get_db()
    try:
        node = public.node_row(db, node_id)
    finally:
        db.close()
    if node["state"] in public.TERMINAL_STATES:
        return str(node["state"])
    return "archived" if node["archived_at"] is not None else None


async def _settle_step(thread_id: str, step: BrowserStep) -> None:
    if step.node_id is None:
        return
    await asyncio.to_thread(
        _plan_write, "settle_user_step", step.node_id,
        evidence=step.settlement_evidence, provenance=step.provenance,
    )
    await _emit(f"{step.event_prefix}_settled", thread_id)


def _plan_write(operation: str, *args: object, **kwargs: object) -> str | None:
    """Run one plans.public step write in its own connection and transaction, off the event loop."""
    public = importlib.import_module("plans.public")
    db = get_db()
    try:
        return getattr(public, operation)(db, *args, **kwargs)
    finally:
        db.close()


async def _emit(action: str, thread_id: str) -> None:
    await asyncio.to_thread(emit_product_event, "browser_pane", action, thread_id=thread_id)


async def _refuse_password_read(guarded: PasswordReadGuard, args: tuple[str, ...], resolve: Resolve | None) -> None:
    """Keep a password visit's DOM reads unavailable after its field is hidden (AR2)."""
    if args and args[0] == "eval":
        raise BrowserSessionError(PASSWORD_PAGE)
    if len(args) >= 3 and args[0] == "get" and args[1] in {"value", "attr"}:
        try:
            node = await resolve(args[2]) if resolve is not None else None
        except Exception as exc:
            log.info("password-visit read target for %s could not be located: %s", guarded.visit, exc)
            node = None
        # A target that can't be located may be the hidden password input itself, so it fails closed.
        if node is None or node in guarded.seen_nodes:
            raise BrowserSessionError(PASSWORD_PAGE)


def password_page(thread_id: str) -> bool:
    """Expose only this thread's password-page boolean to internal consumers."""
    return isinstance(_steps.get(thread_id), PasswordStep)


def is_http_url(url: object) -> bool:
    """Accept only page-navigation URLs the shared browser can safely open."""
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def announce_browser_opened(thread_id: str) -> None:
    """Tell the thread's pages a successful browser open is now streamable."""
    state.broadcast_to_thread(thread_id, {"type": "browser_opened", "thread_id": thread_id})
