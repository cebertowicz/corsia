"""Shared-browser password guard and pane-presence state."""
from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urlparse

from host_access.browser_cdp import PasswordProbe
from host_access.browser_session_state import BrowserSessionError
from infra import state
from infra.database import get_db

PASSWORD_PAGE = (
    "This page asks for a password, so it is the person's to complete. They already see a notice on the "
    "page and a task in their attention list, and your current task waits on it; end your turn. If they "
    "decline, tell them in chat what you needed the page for and stop."
)
PASSWORD_CHECK_FAILED = "This page couldn't be checked"
Probe = Callable[[str | None, frozenset[int]], Awaitable[PasswordProbe]]


@dataclass
class PasswordEpisode:
    """One page-visit password episode, with no page values or text retained."""

    visit: str
    host: str
    seen_nodes: frozenset[int]
    node_id: str | None = None


_panes: dict[str, set[str]] = {}
_pane_reports: dict[str, dict[str, Callable[[dict], None]]] = {}
_episodes: dict[str, PasswordEpisode] = {}
# A completed password episode remains read-guarded until the document loader changes.
_guarded_visits: dict[str, PasswordEpisode] = {}
_probe_locks: dict[str, asyncio.Lock] = {}


def pane_connected(thread_id: str, connection_token: str, report: Callable[[dict], None] | None = None) -> None:
    """Record a shared pane and its relay callback without granting exclusive page control."""
    _panes.setdefault(thread_id, set()).add(connection_token)
    if report is not None:
        _pane_reports.setdefault(thread_id, {})[connection_token] = report


def pane_disconnected(thread_id: str, connection_token: str) -> None:
    """Forget one closed pane connection without changing another pane's presence."""
    reports = _pane_reports.get(thread_id)
    if reports is not None:
        reports.pop(connection_token, None)
        if not reports:
            del _pane_reports[thread_id]
    connections = _panes.get(thread_id)
    if connections is None:
        return
    connections.discard(connection_token)
    if not connections:
        del _panes[thread_id]


def has_pane(thread_id: str) -> bool:
    """Return whether a person can currently see this thread's shared browser."""
    return bool(_panes.get(thread_id))


async def require_page_access(thread_id: str, probe: Probe, args: tuple[str, ...]) -> None:
    """Run one probe and enforce the password-page and per-visit read guards."""
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
            await _settle_episode(thread_id, prior)
            _episodes.pop(thread_id, None)
            _publish_password_page(thread_id, False)
            prior = None
        if result.visible_nodes:
            episode = _episode_for(thread_id, result, prior)
            _guarded_visits[thread_id] = episode
            await _open_episode(thread_id, episode)
            _publish_password_page(thread_id, True)
            ending = " Post nothing in chat." if has_pane(thread_id) else " If you reply, use one short line; don't restate the task or say you made it."
            raise BrowserSessionError(PASSWORD_PAGE + ending)
        if prior is not None:
            await _settle_episode(thread_id, prior)
            _episodes.pop(thread_id, None)
            _publish_password_page(thread_id, False)
        if guarded is not None:
            _refuse_password_read(guarded, args)


def _episode_for(thread_id: str, result: PasswordProbe, prior: PasswordEpisode | None) -> PasswordEpisode:
    if prior is not None and prior.visit == result.visit:
        prior.seen_nodes = prior.seen_nodes | result.visible_nodes
        return prior
    host = result.host or "this site"
    episode = PasswordEpisode(result.visit, host, result.visible_nodes)
    _episodes[thread_id] = episode
    return episode


async def _open_episode(thread_id: str, episode: PasswordEpisode) -> None:
    if episode.node_id is not None:
        return
    public = importlib.import_module("plans.public")
    db = get_db()
    try:
        episode.node_id = public.open_user_step(
            db, thread_id, key=episode.visit, title=f"Enter your password in the browser ({episode.host})",
            description="Enter your password in the browser pane. The agent can't see this page and carries on once you're past it.",
            provenance="browser.password_guard",
        )
    finally:
        db.close()


async def _settle_episode(thread_id: str, episode: PasswordEpisode) -> None:
    if episode.node_id is None:
        return
    public = importlib.import_module("plans.public")
    db = get_db()
    try:
        public.settle_user_step(db, episode.node_id, evidence="Password field no longer on the page",
                                provenance="browser.password_guard")
    finally:
        db.close()


def _refuse_password_read(episode: PasswordEpisode, args: tuple[str, ...]) -> None:
    """Keep a password visit's DOM reads unavailable after its field is hidden."""
    if args and args[0] == "eval":
        raise BrowserSessionError(PASSWORD_PAGE)
    if len(args) >= 3 and args[0] == "get" and args[1] in {"value", "attr"}:
        try:
            node = int(args[2])
        except ValueError:
            return
        if node in episode.seen_nodes:
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
