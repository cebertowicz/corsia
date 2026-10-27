"""Shared-browser page access and the read guard on a password visit."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from host_access import browser_pane_state, browser_step_lifecycle
from host_access.browser_cdp import PasswordProbe
from host_access.browser_session_state import BrowserSessionError
from host_access.browser_url import is_http_url as is_http_url, normalize_browser_url as normalize_browser_url
from infra import state

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
PASSWORD_DECLINED = (
    "The person declined to enter a password on this page. Tell them in chat what you needed the page for and stop."
)
PERSON_STEP_DECLINED = (
    "The person declined this step on this page, so it can't be given to them again. "
    "Tell them in chat what you needed the page for and stop."
)
# Locates an agent's ref or CSS selector on the page as a backendNodeId, or None when it can't.
Resolve = Callable[[str], Awaitable[int | None]]


@dataclass
class PasswordReadGuard:
    """Only node identities retained for a document; never a plan step or page content."""

    visit: str
    seen_nodes: frozenset[int] = frozenset()


# A document stays read-guarded independently of whether any step is open, and a thread keeps one guard
# per document it has guarded: leaving a tab never lifts it, and a loader id never recurs, so a new
# document is never over-refused. Cleared when the thread's browser closes.
_guarded_visits: dict[str, dict[str, PasswordReadGuard]] = {}


def pane_connected(thread_id: str, connection_token: str, report: Callable[[dict], None] | None = None,
                   probe: browser_pane_state.Probe | None = None, *,
                   session: Callable[[], str | None] | None = None) -> None:
    """Record a shared pane and notify the step owner when it can probe."""
    browser_pane_state.pane_connected(
        thread_id, connection_token, report, probe,
        on_probe=lambda thread: browser_step_lifecycle.pane_event(thread, "pane_probe_available"), session=session,
    )


def pane_disconnected(thread_id: str, connection_token: str) -> None:
    """Notify the step owner once no pane remains to probe the page."""
    browser_pane_state.pane_disconnected(
        thread_id, connection_token,
        on_last_probe=lambda thread: browser_step_lifecycle.pane_event(thread, "pane_probe_gone"),
    )


async def require_page_access(thread_id: str, probe: browser_pane_state.Probe, args: tuple[str, ...], *,
                              resolve: Resolve | None = None, session: str | None = None) -> None:
    """Enforce page ownership, then the independent password-visit read guard."""
    result = await _guarded_probe(thread_id, probe, session=session)
    if (guarded := _guarded_visits.get(thread_id, {}).get(result.visit)) is not None:
        await _refuse_password_read(guarded, args, resolve)


async def declare_person_step(thread_id: str, probe: browser_pane_state.Probe, instruction: str, *,
                              session: str | None = None) -> None:
    """Give the page to the person; an open declaration is a no-op, a decline is final for its visit."""
    instruction = str(instruction).strip()
    if not instruction:
        raise BrowserSessionError("browser person step needs an instruction")
    await _guarded_probe(thread_id, probe, session=session, instruction=instruction)


async def _guarded_probe(thread_id: str, probe: browser_pane_state.Probe, *, session: str | None = None,
                         instruction: str | None = None) -> PasswordProbe:
    """Share the agent probe and failure mapping between commands and declarations."""
    try:
        result = await probe(_known_nodes(thread_id))
    except Exception as exc:
        if isinstance(exc, BrowserSessionError) and str(exc) == PAGE_CHECK_FAILED:
            raise
        raise BrowserSessionError(PAGE_CHECK_FAILED) from exc
    event = browser_step_lifecycle.Event(
        "declare" if instruction is not None else "probe", result,
        session=session, instruction=instruction or "",
    )
    decision = await browser_step_lifecycle.step_event(thread_id, event)
    messages = {"password": PASSWORD_PAGE + _ending(thread_id), "person": PERSON_STEP,
                "password_declined": PASSWORD_DECLINED, "person_declined": PERSON_STEP_DECLINED}
    if decision.refusal is not None:
        raise BrowserSessionError(messages[decision.refusal])
    return result


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
    """Note input for the lifecycle owner's re-probe cadence."""
    browser_step_lifecycle.person_input(thread_id)


def browser_closed(thread_id: str) -> None:
    """Clear read guards and agent presence; the session hook owns step cleanup."""
    browser_pane_state.agent_left(thread_id)
    _guarded_visits.pop(thread_id, None)


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
    return browser_step_lifecycle.password_page(thread_id)


def announce_browser_opened(thread_id: str) -> None:
    """Tell the thread's pages a successful browser open is now streamable."""
    state.broadcast_to_thread(thread_id, {"type": "browser_opened", "thread_id": thread_id})
