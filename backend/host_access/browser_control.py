"""Shared-browser password guard and pane-presence state, including each pane's screencast."""
from __future__ import annotations

import asyncio
import importlib
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from urllib.parse import urlparse

from host_access.browser_cdp import PasswordProbe
from host_access.browser_domain import registrable_domain
from host_access.browser_session_state import BrowserSessionError
from host_access.browser_pane_state import (
    _agent_seen, _capture_sizes, _pane_probes, _pane_reports, _panes, _screencasts,
    agent_acted, capture_at, has_pane, page_command_finished, page_command_starting,
    screencast_ended, screencast_started, screencasts,
)
from infra import state
from infra.database import get_db
from infra.product_events import emit_product_event

log = logging.getLogger(__name__)

PASSWORD_PAGE = (
    "This page asks for a password, so it is the person's to complete. They already see a notice on the "
    "page and a task in their attention list, and your current task waits on it; end your turn. If they "
    "decline, tell them in chat what you needed the page for and stop."
)
PASSWORD_CHECK_FAILED = "This page couldn't be checked"
PERSON_STEP = (
    "This browser step is the person's to complete. They already see a task in their attention list, and your "
    "current task waits on it; end your turn."
)
PASSWORD_STEP_DESCRIPTION = (
    "Enter your password in the browser pane. The agent can't see this page and carries on once you're past it.")
PROVENANCE = "browser.password_guard"
PERSON_STEP_PROVENANCE = "browser.person_step"
Probe = Callable[[str | None, frozenset[int]], Awaitable[PasswordProbe]]
# Locates an agent's ref or CSS selector on the page as a backendNodeId, or None when it can't.
Resolve = Callable[[str], Awaitable[int | None]]
# AR6: while an episode is open and a pane is connected, the probe re-runs this many seconds after the
# person's last input, and at least this often.
REPROBE_AFTER_INPUT = 0.5
REPROBE_EVERY = 2.0


@dataclass
class PasswordEpisode:
    """One page-visit password episode, with no page values or text retained."""

    visit: str
    host: str
    seen_nodes: frozenset[int]
    # The page target the episode's visit is on; a probe of any other target never settles it.
    target: str = ""
    node_id: str | None = None
    title: str | None = None
    description: str | None = None
    provenance: str = PROVENANCE
    settlement_evidence: str = "Password field no longer on the page"
    event_prefix: str = "password_step"
    password: bool = True
    # Set once the person's decline (AR7) has been counted, so a resumed re-probe never counts it again.
    declined: bool = False
    # The episode's one re-probe loop, and the person's latest input that it waits to pause (AR6).
    reprobe: asyncio.Task | None = field(default=None, repr=False)
    last_input: float | None = None
    input_seen: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


# The pane dictionaries stay re-exported for the existing browser-pane test seam.
_episodes: dict[str, PasswordEpisode] = {}
# A completed password episode remains read-guarded until the document loader changes.
_guarded_visits: dict[str, PasswordEpisode] = {}
_probe_locks: dict[str, asyncio.Lock] = {}


def pane_connected(thread_id: str, connection_token: str, report: Callable[[dict], None] | None = None,
                   probe: Probe | None = None) -> None:
    from host_access import browser_pane_state

    browser_pane_state.pane_connected(
        thread_id, connection_token, report, probe,
        on_probe=lambda thread: _start_reprobe(thread, _episodes[thread]) if thread in _episodes else None,
    )


def pane_disconnected(thread_id: str, connection_token: str) -> None:
    from host_access import browser_pane_state

    browser_pane_state.pane_disconnected(
        thread_id, connection_token,
        on_last_probe=lambda thread: _stop_reprobe(_episodes[thread]) if thread in _episodes else None,
    )


async def require_page_access(thread_id: str, probe: Probe, args: tuple[str, ...], *,
                              resolve: Resolve | None = None) -> None:
    """Run one probe and enforce the password-page and per-visit read guards.

    ``resolve`` locates a value or attribute read's target, so a read of a hidden password input is
    refused (AR2); without it, such a read on a guarded visit is refused.
    """
    lock = _probe_locks.setdefault(thread_id, asyncio.Lock())
    async with lock:
        prior = _episodes.get(thread_id)
        known_visit, known = (prior.visit, prior.seen_nodes) if prior is not None else (None, frozenset())
        try:
            result = await probe(known_visit, known)
        except Exception as exc:
            if isinstance(exc, BrowserSessionError) and str(exc) == PASSWORD_CHECK_FAILED:
                raise
            raise BrowserSessionError(PASSWORD_CHECK_FAILED) from exc
        guarded = _guarded_visits.get(thread_id)
        if guarded is not None and guarded.visit != result.visit:
            _guarded_visits.pop(thread_id, None)
            guarded = None
        if prior is not None and prior.visit != result.visit:
            await _end_episode(thread_id, prior)
            prior = None
        if result.visible_nodes:
            episode = _episode_for(thread_id, result, prior)
            _guarded_visits[thread_id] = episode
            await _open_episode(thread_id, episode)
            _start_reprobe(thread_id, episode)
            _publish_password_page(thread_id, True)
            ending = " Post nothing in chat." if has_pane(thread_id) else " If you reply, use one short line; don't restate the task or say you made it."
            raise BrowserSessionError(PASSWORD_PAGE + ending)
        if prior is not None and not prior.password:
            # The person's step is still on this visit: the agent's command never touches the page or settles it.
            raise BrowserSessionError(PERSON_STEP)
        if prior is not None:
            await _end_episode(thread_id, prior)
        if guarded is not None:
            await _refuse_password_read(guarded, args, resolve)


async def declare_person_step(thread_id: str, probe: Probe, instruction: str) -> None:
    """Open a system-observed person step for a visible browser page, then end the agent turn."""
    instruction = str(instruction).strip()
    if not instruction:
        raise BrowserSessionError("browser person step needs an instruction")
    lock = _probe_locks.setdefault(thread_id, asyncio.Lock())
    async with lock:
        prior = _episodes.get(thread_id)
        known_visit, known = (prior.visit, prior.seen_nodes) if prior is not None else (None, frozenset())
        try:
            result = await probe(known_visit, known)
        except Exception as exc:
            if isinstance(exc, BrowserSessionError) and str(exc) == PASSWORD_CHECK_FAILED:
                raise
            raise BrowserSessionError(PASSWORD_CHECK_FAILED) from exc
        if prior is not None and prior.visit != result.visit:
            await _end_episode(thread_id, prior)
            prior = None
        if result.visible_nodes:
            episode = _episode_for(thread_id, result, prior)
            _guarded_visits[thread_id] = episode
            await _open_episode(thread_id, episode)
            _start_reprobe(thread_id, episode)
            _publish_password_page(thread_id, True)
            raise BrowserSessionError(PASSWORD_PAGE)
        domain = registrable_domain(result.host) or "this site"
        episode = _episode_for(thread_id, result, prior)
        episode.title = f"{instruction} ({domain})"
        episode.description = (
            f"{instruction} in the browser pane. The agent can't see this page and carries on once you're past it."
        )
        episode.provenance = PERSON_STEP_PROVENANCE
        episode.settlement_evidence = "The browser page changed after the person completed this step"
        episode.event_prefix = "person_step"
        episode.password = False
        await _open_episode(thread_id, episode)
        _start_reprobe(thread_id, episode)
        raise BrowserSessionError(PERSON_STEP)


def person_input(thread_id: str) -> None:
    """Note the person's pane input, so an open episode re-probes once that input pauses (AR6)."""
    episode = _episodes.get(thread_id)
    if episode is not None and episode.reprobe is not None:
        episode.last_input = asyncio.get_running_loop().time()
        episode.input_seen.set()


def browser_closed(thread_id: str) -> None:
    """Stop an open episode's re-probe once the thread's browser has closed; the agent is in no page."""
    _agent_seen.pop(thread_id, None)
    if (episode := _episodes.get(thread_id)) is not None:
        _stop_reprobe(episode)


def _start_reprobe(thread_id: str, episode: PasswordEpisode) -> None:
    if _pane_probes.get(thread_id) and (episode.reprobe is None or episode.reprobe.done()):
        episode.reprobe = asyncio.create_task(_reprobe_loop(thread_id, episode))


def _stop_reprobe(episode: PasswordEpisode) -> None:
    task, episode.reprobe = episode.reprobe, None
    if task is not None and task is not asyncio.current_task():
        task.cancel()


async def _reprobe_loop(thread_id: str, episode: PasswordEpisode) -> None:
    """Re-run the probe once the person's input pauses and every REPROBE_EVERY seconds, until the episode ends."""
    clock = asyncio.get_running_loop().time
    while True:
        next_probe = clock() + REPROBE_EVERY
        while True:
            # Cleared before the deadline is read, so input arriving after this always moves it.
            episode.input_seen.clear()
            due = next_probe if episode.last_input is None else min(next_probe, episode.last_input + REPROBE_AFTER_INPUT)
            try:
                await asyncio.wait_for(episode.input_seen.wait(), max(0.0, due - clock()))
            except TimeoutError:
                break
        episode.last_input = None
        if not await _reprobe(thread_id, episode):
            return


async def _reprobe(thread_id: str, episode: PasswordEpisode) -> bool:
    """Probe once for an open episode; return whether it is still open and its loop should go on."""
    async with _probe_locks.setdefault(thread_id, asyncio.Lock()):
        probes = list(_pane_probes.get(thread_id, {}).values())
        if _episodes.get(thread_id) is not episode or not probes:
            return False
        if await _step_ended(episode):
            if not episode.declined:
                episode.declined = True
                await _emit(f"{episode.event_prefix}_declined", thread_id)
            return False
        try:
            result = await probes[0](episode.visit, episode.seen_nodes)
        except Exception as exc:
            # A page mid-navigation can fail one probe. The episode stays open, as an agent command's
            # failed probe fails closed, and the next interval probes again.
            log.info("password re-probe for %s failed: %s", thread_id, exc)
            return True
        if result.target != episode.target:
            log.info("browser re-probe for %s reached another page target", thread_id)
            episode.settlement_evidence = "The person moved to another browser tab"
            await _end_episode(thread_id, episode)
            return False
        if result.visit == episode.visit and result.visible_nodes:
            episode.seen_nodes = episode.seen_nodes | result.visible_nodes
            return True
        if (guarded := _guarded_visits.get(thread_id)) is not None and guarded.visit != result.visit:
            _guarded_visits.pop(thread_id, None)
        await _end_episode(thread_id, episode)
        return False


def _episode_for(thread_id: str, result: PasswordProbe, prior: PasswordEpisode | None) -> PasswordEpisode:
    if prior is not None and prior.visit == result.visit:
        prior.seen_nodes = prior.seen_nodes | result.visible_nodes
        return prior
    episode = PasswordEpisode(result.visit, result.host, result.visible_nodes, result.target)
    _episodes[thread_id] = episode
    return episode


async def _open_episode(thread_id: str, episode: PasswordEpisode) -> None:
    if episode.node_id is not None:
        return
    # AR5: the title names the live page's registrable domain, the one the pane's address keeps visible.
    domain = registrable_domain(episode.host) or "this site"
    title = episode.title or f"Enter your password in the browser ({domain})"
    description = episode.description or PASSWORD_STEP_DESCRIPTION
    episode.node_id = await asyncio.to_thread(
        _plan_write, "open_user_step", thread_id, key=episode.visit,
        title=title, description=description, provenance=episode.provenance,
    )
    await _emit(f"{episode.event_prefix}_opened", thread_id)


async def _end_episode(thread_id: str, episode: PasswordEpisode) -> None:
    _stop_reprobe(episode)
    await _settle_episode(thread_id, episode)
    _episodes.pop(thread_id, None)
    if episode.password:
        _publish_password_page(thread_id, False)


async def _step_ended(episode: PasswordEpisode) -> bool:
    """Whether the person declined the episode's step or it otherwise closed (AR7)."""
    if episode.node_id is None:
        return False
    return await asyncio.to_thread(_step_row_ended, episode.node_id)


def _step_row_ended(node_id: str) -> bool:
    public = importlib.import_module("plans.public")
    db = get_db()
    try:
        node = public.node_row(db, node_id)
    finally:
        db.close()
    return node["archived_at"] is not None or node["state"] in public.TERMINAL_STATES


async def _settle_episode(thread_id: str, episode: PasswordEpisode) -> None:
    if episode.node_id is None:
        return
    await asyncio.to_thread(
        _plan_write, "settle_user_step", episode.node_id,
        evidence=episode.settlement_evidence, provenance=episode.provenance,
    )
    await _emit(f"{episode.event_prefix}_settled", thread_id)


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


async def _refuse_password_read(episode: PasswordEpisode, args: tuple[str, ...], resolve: Resolve | None) -> None:
    """Keep a password visit's DOM reads unavailable after its field is hidden (AR2)."""
    if args and args[0] == "eval":
        raise BrowserSessionError(PASSWORD_PAGE)
    if len(args) >= 3 and args[0] == "get" and args[1] in {"value", "attr"}:
        try:
            node = await resolve(args[2]) if resolve is not None else None
        except Exception as exc:
            log.info("password-visit read target for %s could not be located: %s", episode.visit, exc)
            node = None
        # A target that can't be located may be the hidden password input itself, so it fails closed.
        if node is None or node in episode.seen_nodes:
            raise BrowserSessionError(PASSWORD_PAGE)


def password_page(thread_id: str) -> bool:
    """Expose only this thread's password-page boolean to internal consumers."""
    return bool((episode := _episodes.get(thread_id)) and episode.password)


def _publish_password_page(thread_id: str, visible: bool) -> None:
    message = {"type": "browser_password_page", "thread_id": thread_id, "visible": visible}
    state.broadcast_to_thread(thread_id, message)
    for report in _pane_reports.get(thread_id, {}).values():
        report(message)


def is_http_url(url: object) -> bool:
    """Accept only page-navigation URLs the shared browser can safely open."""
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def announce_browser_opened(thread_id: str) -> None:
    """Tell the thread's pages a successful browser open is now streamable."""
    state.broadcast_to_thread(thread_id, {"type": "browser_opened", "thread_id": thread_id})
