"""Shared-browser password guard and pane-presence state, including each pane's screencast."""
from __future__ import annotations

import asyncio
import importlib
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from urllib.parse import urlparse

from host_access.browser_cdp import PasswordProbe, Rect
from host_access.browser_domain import registrable_domain
from host_access.browser_screencast import PaneScreencast
from host_access.browser_session_state import BrowserSessionError
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
PASSWORD_STEP_DESCRIPTION = (
    "Enter your password in the browser pane. The agent can't see this page and carries on once you're past it.")
PROVENANCE = "browser.password_guard"
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
    # Set once the person's decline (AR7) has been counted, so a resumed re-probe never counts it again.
    declined: bool = False
    # The episode's one re-probe loop, and the person's latest input that it waits to pause (AR6).
    reprobe: asyncio.Task | None = field(default=None, repr=False)
    last_input: float | None = None
    input_seen: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


_panes: dict[str, set[str]] = {}
_capture_sizes: dict[str, tuple[int, int, bool]] = {}
_pane_reports: dict[str, dict[str, Callable[[dict], None]]] = {}
# Each connected pane's probe of the shared page, over the page endpoint it already holds.
_pane_probes: dict[str, dict[str, Probe]] = {}
_episodes: dict[str, PasswordEpisode] = {}
# A completed password episode remains read-guarded until the document loader changes.
_guarded_visits: dict[str, PasswordEpisode] = {}
_probe_locks: dict[str, asyncio.Lock] = {}
_screencasts: dict[str, dict[str, PaneScreencast]] = {}
# When the agent last acted in each thread's page, so a pane that connects later shows it there.
_agent_seen: dict[str, float] = {}
# agent-browser commands that change the page's device metrics; a screenshot changes them while it captures.
_METRICS_COMMANDS = {("screenshot",), ("set", "viewport"), ("set", "device")}


def pane_connected(thread_id: str, connection_token: str, report: Callable[[dict], None] | None = None,
                   probe: Probe | None = None) -> None:
    """Record a shared pane, its relay callback and its page probe without granting exclusive page control."""
    _panes.setdefault(thread_id, set()).add(connection_token)
    if report is not None:
        _pane_reports.setdefault(thread_id, {})[connection_token] = report
        if (seen := _agent_seen.get(thread_id)) is not None:
            report(_agent_message(time.monotonic() - seen))
    if probe is not None:
        _pane_probes.setdefault(thread_id, {})[connection_token] = probe
        if (episode := _episodes.get(thread_id)) is not None:
            _start_reprobe(thread_id, episode)


def pane_disconnected(thread_id: str, connection_token: str) -> None:
    """Forget one closed pane connection without changing another pane's presence."""
    reports = _pane_reports.get(thread_id)
    if reports is not None:
        reports.pop(connection_token, None)
        if not reports:
            del _pane_reports[thread_id]
    probes = _pane_probes.get(thread_id)
    if probes is not None:
        probes.pop(connection_token, None)
        if not probes:
            del _pane_probes[thread_id]
            if (episode := _episodes.get(thread_id)) is not None:
                _stop_reprobe(episode)
    connections = _panes.get(thread_id)
    if connections is None:
        return
    connections.discard(connection_token)
    if not connections:
        del _panes[thread_id]
        _capture_sizes.pop(thread_id, None)


def has_pane(thread_id: str) -> bool:
    """Return whether a person can currently see this thread's shared browser."""
    return bool(_panes.get(thread_id))


def screencast_started(thread_id: str, connection_token: str, cast: PaneScreencast) -> None:
    _screencasts.setdefault(thread_id, {})[connection_token] = cast
    if (size := _capture_sizes.get(thread_id)) is not None:
        cast.capture_at(size[0], size[1], compact=size[2])


def capture_at(thread_id: str, width: int, height: int, *, compact: bool) -> None:
    """Size current and not-yet-connected screencasts from the pane's latest appearance."""
    _capture_sizes[thread_id] = (width, height, compact)
    for cast in screencasts(thread_id):
        cast.capture_at(width, height, compact=compact)


def screencast_ended(thread_id: str, connection_token: str, cast: PaneScreencast) -> None:
    casts = _screencasts.get(thread_id, {})
    if casts.get(connection_token) is cast:
        del casts[connection_token]
        if not casts:
            del _screencasts[thread_id]


def screencasts(thread_id: str) -> list[PaneScreencast]:
    """Every pane screencast showing this thread's one shared page."""
    return list(_screencasts.get(thread_id, {}).values())


def agent_acted(thread_id: str, field: Rect | None = None, label: Rect | None = None) -> None:
    """Show the thread's panes that its agent is in the page, and the field it wrote, if any."""
    _agent_seen[thread_id] = time.monotonic()
    message = _agent_message(0.0, field, label)
    for report in _pane_reports.get(thread_id, {}).values():
        report(message)


def _agent_message(age: float, field: Rect | None = None, label: Rect | None = None) -> dict:
    # An age rather than a time, so the pane needs no clock agreement with this server.
    return {"type": "browser_agent", "age_ms": round(age * 1000), "field": field, "label": label}


def page_command_starting(thread_id: str, args: tuple[str, ...]) -> None:
    """Hold the thread's pane pictures while a command changes the page's metrics."""
    if _changes_metrics(args):
        for cast in screencasts(thread_id):
            cast.hold()


async def page_command_finished(thread_id: str, args: tuple[str, ...]) -> None:
    """Restart the thread's pane screencasts after a command that changed the page's metrics."""
    if _changes_metrics(args):
        for cast in screencasts(thread_id):
            await cast.restart()


def _changes_metrics(args: tuple[str, ...]) -> bool:
    return args[:1] in _METRICS_COMMANDS or args[:2] in _METRICS_COMMANDS


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
        if prior is not None:
            await _end_episode(thread_id, prior)
        if guarded is not None:
            await _refuse_password_read(guarded, args, resolve)


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
                await _emit("password_step_declined", thread_id)
            return False
        try:
            result = await probes[0](episode.visit, episode.seen_nodes)
        except Exception as exc:
            # A page mid-navigation can fail one probe. The episode stays open, as an agent command's
            # failed probe fails closed, and the next interval probes again.
            log.info("password re-probe for %s failed: %s", thread_id, exc)
            return True
        if result.target != episode.target:
            # The pane shows another tab than the one the agent was refused on; only the agent's own
            # next command, which probes its page, can end this episode.
            log.info("password re-probe for %s reached another page target", thread_id)
            return True
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
    episode.node_id = await asyncio.to_thread(_plan_write, "open_user_step", thread_id, key=episode.visit,
                                              title=f"Enter your password in the browser ({domain})",
                                              description=PASSWORD_STEP_DESCRIPTION, provenance=PROVENANCE)
    await _emit("password_step_opened", thread_id)


async def _end_episode(thread_id: str, episode: PasswordEpisode) -> None:
    _stop_reprobe(episode)
    await _settle_episode(thread_id, episode)
    _episodes.pop(thread_id, None)
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
    await asyncio.to_thread(_plan_write, "settle_user_step", episode.node_id,
                            evidence="Password field no longer on the page", provenance=PROVENANCE)
    await _emit("password_step_settled", thread_id)


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
    return thread_id in _episodes


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
