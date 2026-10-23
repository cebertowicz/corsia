"""Shared-browser page guard: the person's password and person steps, and the read guard on a password visit."""
from __future__ import annotations

import asyncio
import importlib
import logging
from typing import Awaitable, Callable
from urllib.parse import urlparse

from host_access import browser_pane_state
from host_access.browser_cdp import PasswordProbe
from host_access.browser_pane_state import (
    Probe, agent_acted, capture_at, has_pane, page_command_finished, page_command_starting,
    screencast_ended, screencast_started, screencasts,
)
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
# Locates an agent's ref or CSS selector on the page as a backendNodeId, or None when it can't.
Resolve = Callable[[str], Awaitable[int | None]]
# AR6: while a step is open and a pane is connected, the probe re-runs this many seconds after the
# person's last input, and at least this often.
REPROBE_AFTER_INPUT = 0.5
REPROBE_EVERY = 2.0

_steps: dict[str, BrowserStep] = {}
# A password visit stays read-guarded until the document loader changes, after its step has ended too.
_guarded_visits: dict[str, PasswordStep] = {}
_probe_locks: dict[str, asyncio.Lock] = {}


def pane_connected(thread_id: str, connection_token: str, report: Callable[[dict], None] | None = None,
                   probe: Probe | None = None) -> None:
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


async def require_page_access(thread_id: str, probe: Probe, args: tuple[str, ...], *,
                              resolve: Resolve | None = None) -> None:
    """Run one probe and enforce the password-page, person-step and per-visit read guards.

    ``resolve`` locates a value or attribute read's target, so a read of a hidden password input is
    refused (AR2); without it, such a read on a guarded visit is refused.
    """
    async with _probe_locks.setdefault(thread_id, asyncio.Lock()):
        _result, prior = await _guarded_probe(thread_id, probe)
        if isinstance(prior, PersonStep):
            # The person's step is still on this visit: the agent's command never touches the page or settles it.
            raise BrowserSessionError(PERSON_STEP)
        if prior is not None:
            await _end_step(thread_id, prior)
        if (guarded := _guarded_visits.get(thread_id)) is not None:
            await _refuse_password_read(guarded, args, resolve)


async def declare_person_step(thread_id: str, probe: Probe, instruction: str) -> None:
    """Give the current page to the person as a step that settles on their next page visit.

    A password page is refused exactly as any other command is; a second declaration on the visit is a no-op.
    """
    instruction = str(instruction).strip()
    if not instruction:
        raise BrowserSessionError("browser person step needs an instruction")
    async with _probe_locks.setdefault(thread_id, asyncio.Lock()):
        result, prior = await _guarded_probe(thread_id, probe)
        if isinstance(prior, PasswordStep):
            raise BrowserSessionError(PASSWORD_PAGE + _ending(thread_id))
        if prior is not None:
            return
        step = PersonStep(result.visit, result.host, result.target, instruction=instruction)
        _steps[thread_id] = step
        await _open_step(thread_id, step)
        _start_reprobe(thread_id, step)


async def _guarded_probe(thread_id: str, probe: Probe) -> tuple[PasswordProbe, BrowserStep | None]:
    """Probe the agent's page under its lock; return the result and the step still open on its visit.

    A new visit ends the prior step and lifts the read guard. A person step the person has ended is
    dropped, and a visible password field opens or keeps the visit's password step and is refused.
    """
    prior = _steps.get(thread_id)
    known_visit, known = (prior.visit, _known_nodes(thread_id, prior.visit)) if prior is not None else (None, frozenset())
    try:
        result = await probe(known_visit, known)
    except Exception as exc:
        if isinstance(exc, BrowserSessionError) and str(exc) == PAGE_CHECK_FAILED:
            raise
        raise BrowserSessionError(PAGE_CHECK_FAILED) from exc
    if (guarded := _guarded_visits.get(thread_id)) is not None and guarded.visit != result.visit:
        _guarded_visits.pop(thread_id, None)
    if prior is not None and prior.visit != result.visit:
        await _end_step(thread_id, prior)
        prior = None
    if isinstance(prior, PersonStep):
        if result.visible_nodes:
            _guard(thread_id, result)
        if not await _row_ended(thread_id, prior):
            return result, prior
        prior = None
    if result.visible_nodes:
        # A password step is its visit's guard, so an open one on this visit is the same step.
        step = _guard(thread_id, result)
        _steps[thread_id] = step
        await _open_step(thread_id, step)
        _start_reprobe(thread_id, step)
        step.shown(thread_id)
        raise BrowserSessionError(PASSWORD_PAGE + _ending(thread_id))
    return result, prior


def _ending(thread_id: str) -> str:
    if has_pane(thread_id):
        return " Post nothing in chat."
    return " If you reply, use one short line; don't restate the task or say you made it."


def _guard(thread_id: str, result: PasswordProbe) -> PasswordStep:
    """Keep the visit's visible password nodes read-guarded until its document loader changes (AR2)."""
    guarded = _guarded_visits.get(thread_id)
    if guarded is None or guarded.visit != result.visit:
        guarded = _guarded_visits[thread_id] = PasswordStep(result.visit, result.host, result.target)
    guarded.seen_nodes = guarded.seen_nodes | result.visible_nodes
    return guarded


def _known_nodes(thread_id: str, visit: str) -> frozenset[int]:
    guarded = _guarded_visits.get(thread_id)
    return guarded.seen_nodes if guarded is not None and guarded.visit == visit else frozenset()


def person_input(thread_id: str) -> None:
    """Note the person's pane input, so an open step re-probes once that input pauses (AR6)."""
    step = _steps.get(thread_id)
    if step is not None and step.reprobe is not None:
        step.last_input = asyncio.get_running_loop().time()
        step.input_seen.set()


def browser_closed(thread_id: str) -> None:
    """Stop an open step's re-probe once the thread's browser has closed; the agent is in no page."""
    browser_pane_state.agent_left(thread_id)
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
        if not await _reprobe(thread_id, step):
            return


async def _reprobe(thread_id: str, step: BrowserStep) -> bool:
    """Probe once for an open step; return whether it is still open and its loop should go on."""
    async with _probe_locks.setdefault(thread_id, asyncio.Lock()):
        probes = browser_pane_state.pane_probes(thread_id)
        current = _steps.get(thread_id)
        if current is None or current is not step or not probes:
            return False
        if await _row_ended(thread_id, step):
            return False
        try:
            result = await probes[0](step.visit, _known_nodes(thread_id, step.visit))
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
        if result.visit == step.visit and result.visible_nodes:
            _guard(thread_id, result)
        if step.still_open(result):
            return True
        if (guarded := _guarded_visits.get(thread_id)) is not None and guarded.visit != result.visit:
            _guarded_visits.pop(thread_id, None)
        await _end_step(thread_id, step)
        return False


async def _open_step(thread_id: str, step: BrowserStep) -> None:
    if step.node_id is not None:
        return
    step.node_id = await asyncio.to_thread(
        _plan_write, "open_user_step", thread_id, key=step.visit,
        title=step.title(), description=step.description(), provenance=step.provenance,
    )
    await _emit(f"{step.event_prefix}_opened", thread_id)


async def _end_step(thread_id: str, step: BrowserStep) -> None:
    _stop_reprobe(step)
    await _settle_step(thread_id, step)
    _steps.pop(thread_id, None)
    step.ended(thread_id)


async def _row_ended(thread_id: str, step: BrowserStep) -> bool:
    """Whether the person ended the step's row themselves; a decline is counted once (AR7).

    A person step they ended is dropped, so the agent's next command runs. A password page stays
    guarded while its field shows.
    """
    if step.node_id is None:
        return False
    row_state = await asyncio.to_thread(_step_row_state, step.node_id)
    if row_state is None:
        return False
    if row_state not in DONE_STATES and not step.declined:
        step.declined = True
        await _emit(f"{step.event_prefix}_declined", thread_id)
    if isinstance(step, PersonStep):
        _stop_reprobe(step)
        _steps.pop(thread_id, None)
    return True


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


async def _refuse_password_read(guarded: PasswordStep, args: tuple[str, ...], resolve: Resolve | None) -> None:
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
