"""Authenticated relay between the browser pane and the shared page it shows."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
from functools import partial
from typing import Callable

from fastapi import WebSocket, WebSocketDisconnect
from websockets.exceptions import WebSocketException

from host_access import browser_control, browser_pane
from host_access.browser_domain import url_domain
from host_access.browser_session_state import BrowserSessionError
from host_access.processes import ProcessOwnerError
from infra.auth import authenticate, narrow_to_listener
from infra.database import get_db
from infra.queries import get_thread_for_user
from ws.browser_frames import PaneFrames

log = logging.getLogger(__name__)
# Seconds before each re-establishment of either CDP connection; a stable run resets the budget.
PANE_RETRY_DELAYS = (0.5, 1.0, 2.0)
PANE_RETRY_RESET_SECONDS = 30.0


async def browser_stream_endpoint(ws: WebSocket, thread_id: str) -> None:
    """Relay one owned thread's private stream without joining chat socket state."""
    await ws.accept()
    user = await _authenticate_pane_stream(ws)
    if user is None:
        return
    if not _user_owns_thread(thread_id, user["id"]):
        await ws.close(code=1008)
        return
    if not await browser_pane.is_open(thread_id):
        await ws.send_json({"type": "browser_state", "state": "no_browser_yet"})
        await ws.close()
        return
    try:
        await _relay(ws, thread_id, user["id"])
    except WebSocketDisconnect:
        pass
    except (BrowserSessionError, ProcessOwnerError) as exc:
        await _close_with_error(ws, str(exc))
    except (OSError, WebSocketException):
        await _close_with_error(ws, "browser stream connection failed")


async def _close_with_error(ws: WebSocket, reason: str) -> None:
    # The pane may already be gone; its own close handler then clears its control state.
    with contextlib.suppress(Exception):
        await ws.send_json({"type": "browser_state", "state": "error", "reason": reason})
        await ws.close()


async def _authenticate_pane_stream(ws: WebSocket) -> dict | None:
    """Authenticate the first pane message without chat presence registration."""
    try:
        message = await asyncio.wait_for(ws.receive_json(), timeout=10)
        token = message.get("token", "") if isinstance(message, dict) else ""
        user = narrow_to_listener(ws, authenticate(token))
    except WebSocketDisconnect:
        return None  # The pane left first; there is no socket left to close.
    except (TimeoutError, ValueError):
        user = None
    if not user:
        await ws.close(code=1008)
        return None
    return user


def _user_owns_thread(thread_id: str, user_id: str) -> bool:
    db = get_db()
    try:
        return bool(get_thread_for_user(thread_id, user_id, db=db))
    finally:
        db.close()


async def _screencast_events(thread_id: str, connection_token: str, frames: PaneFrames) -> None:
    """Reconnect the pane frame stream after a transient CDP failure."""
    failures = 0
    while True:
        delivered, started = False, asyncio.get_running_loop().time()

        def deliver(frame: str) -> object:
            nonlocal delivered
            delivered = True
            return frames.offer(frame)

        try:
            await browser_pane.screencast(thread_id, connection_token, deliver)
        except (OSError, WebSocketException) as exc:
            if delivered or asyncio.get_running_loop().time() - started >= PANE_RETRY_RESET_SECONDS:
                failures = 0
            failures += 1
            log.warning("browser pane %s lost its screencast (attempt %d of %d): %s",
                        thread_id, failures, len(PANE_RETRY_DELAYS), exc)
            if failures == len(PANE_RETRY_DELAYS):
                raise BrowserSessionError("The pane stopped showing the page") from exc
        else:
            failures = 0  # A closed page target can be replaced by the session's current target.
        browser_pane.clear_page_endpoint(thread_id, connection_token)
        await asyncio.sleep(PANE_RETRY_DELAYS[max(failures - 1, 0)])


async def _metadata_events(thread_id: str, connection_token: str, report: Callable[[dict], None]) -> None:
    """Reconnect page-address metadata while the pane stays connected."""
    failures = 0
    while True:
        reported, started = False, asyncio.get_running_loop().time()

        async def emit(message: dict) -> None:
            nonlocal reported
            reported = True
            report(message)

        try:
            await browser_pane.watch_page_metadata(thread_id, connection_token, emit)
        except (BrowserSessionError, ProcessOwnerError, OSError, WebSocketException) as exc:
            if reported or asyncio.get_running_loop().time() - started >= PANE_RETRY_RESET_SECONDS:
                failures = 0
            failures += 1
            log.warning("browser pane %s lost its page address watch (attempt %d of %d): %s",
                        thread_id, failures, len(PANE_RETRY_DELAYS), exc)
            if failures == len(PANE_RETRY_DELAYS):
                raise BrowserSessionError("The pane stopped following the page's address") from exc
        else:
            failures = 0
        browser_pane.clear_page_endpoint(thread_id, connection_token)
        await asyncio.sleep(PANE_RETRY_DELAYS[max(failures - 1, 0)])


async def _relay(pane: WebSocket, thread_id: str, user_id: str) -> None:
    """Relay shared page frames and pane input for one authenticated connection."""
    connection_token = secrets.token_urlsafe(18)

    frames = PaneFrames()
    overflow = asyncio.Event()

    def report(message: dict) -> None:
        # A result landing after this relay exits fills a queue nobody sends; that is harmless.
        if not frames.control(message):
            overflow.set()

    browser_control.pane_connected(thread_id, connection_token, report,
                                   partial(browser_pane.password_probe, thread_id, connection_token))

    async def from_pane() -> None:
        while True:
            message = await pane.receive_text()
            try:
                data = json.loads(message)
            except (TypeError, ValueError):
                continue
            kind = data.get("type") if isinstance(data, dict) else None
            if kind == "navigate":
                _request_navigate(thread_id, connection_token, data, report)
            elif kind == "appearance":
                _request_match(thread_id, connection_token, data)
            elif kind == "input_mouse":
                # CDP serializes pointer and key delivery on the page target, so typing cannot
                # overtake the click that focuses its field.
                await browser_pane.mouse_event(thread_id, data, connection_token)
                browser_control.person_input(thread_id)
            elif kind == "input_keyboard":
                await browser_pane.key_event(thread_id, data, connection_token)
                browser_control.person_input(thread_id)

    tasks: set[asyncio.Task] = set()
    try:
        # Shared panes have no control-holder state.
        tasks = {
            asyncio.create_task(_screencast_events(thread_id, connection_token, frames)),
            asyncio.create_task(from_pane()),
            asyncio.create_task(_metadata_events(thread_id, connection_token, report)),
            asyncio.create_task(frames.send(pane.send_text)),
            asyncio.create_task(_close_on_overflow(pane, overflow)),
        }
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # A closed connection's size must never land after a newer pane's.
        if (queued := _latest_appearance.get(thread_id)) and queued[0] == connection_token:
            del _latest_appearance[thread_id]
        browser_pane.clear_page_endpoint(thread_id, connection_token)
        browser_control.pane_disconnected(thread_id, connection_token)


async def _close_on_overflow(pane: WebSocket, overflow: asyncio.Event) -> None:
    """Close the pane once any reliable output path exhausts its bounded queue."""
    await overflow.wait()
    with contextlib.suppress(Exception):
        await pane.close(code=1013)


# Page commands wait behind any running browser command, so one runner per thread runs them beside
# every input loop. Only the newest appearance from any pane connection is applied, and at most one
# navigation waits: a newer one replaces it. A running command is never cancelled: cancelling it
# resets the shared session. A navigation carries its own pane's report, so no relay owns the runner.
_latest_appearance: dict[str, tuple[str, browser_pane.PaneAppearance]] = {}
_pending_navigation: dict[str, tuple[str, dict, Callable[[dict], None]]] = {}
_runners: dict[str, asyncio.Task] = {}


def _request_match(thread_id: str, connection_token: str, data: dict) -> None:
    if (appearance := browser_pane.PaneAppearance.parse(data)) is None:
        return
    _latest_appearance[thread_id] = (connection_token, appearance)
    _run_page_commands(thread_id)


def _request_navigate(thread_id: str, connection_token: str, data: dict, report: Callable[[dict], None]) -> None:
    if (replaced := _pending_navigation.get(thread_id)) is not None:
        replaced[2]({"type": "navigate_result", "error": "Superseded by a newer navigation"})
    _pending_navigation[thread_id] = (connection_token, data, report)
    _run_page_commands(thread_id)


def _run_page_commands(thread_id: str) -> None:
    if (runner := _runners.get(thread_id)) is None or runner.done():
        runner = _runners[thread_id] = asyncio.create_task(_run_pending(thread_id))
        runner.add_done_callback(lambda done: _runners.pop(thread_id) if _runners.get(thread_id) is done else None)


async def _run_pending(thread_id: str) -> None:
    """Navigate, then match the page to the newest pane; a refusal keeps the old size, which the pane letterboxes."""
    while True:
        if (navigation := _pending_navigation.pop(thread_id, None)) is not None:
            connection_token, data, report = navigation
            try:
                result = await _navigate(thread_id, connection_token, data)
            except Exception:
                log.exception("browser pane navigation failed")
                result = {"type": "navigate_result", "error": "Browser navigation failed"}
            report(result)
        elif (queued := _latest_appearance.pop(thread_id, None)) is not None:
            connection_token, appearance = queued
            try:
                await browser_pane.match_pane(thread_id, appearance)
            except (BrowserSessionError, ProcessOwnerError) as exc:
                log.warning("browser pane %s kept its old appearance: %s", thread_id, exc)
        else:
            return


async def _navigate(thread_id: str, connection_token: str, data: dict) -> dict:
    """Open a URL, or go back, forward or reload from an authenticated pane connection."""
    action, url = data.get("action"), data.get("url")
    if action is None and not browser_control.is_http_url(url):
        return {"type": "navigate_result", "error": "Browser URL must be http or https"}
    if action is not None and action not in browser_pane.HISTORY_ACTIONS:
        return {"type": "navigate_result", "error": "Unknown browser action"}
    try:
        if action is None:
            result = await browser_pane.open_url(thread_id, str(url), connection_token)
        else:
            result = await browser_pane.go(thread_id, action)
    except (BrowserSessionError, ProcessOwnerError) as exc:
        return {"type": "navigate_result", "error": str(exc)}
    if result.startswith("Error:"):
        return {"type": "navigate_result", "error": result}
    try:
        current_url, title = await browser_pane.page_info(thread_id)
    except (BrowserSessionError, ProcessOwnerError) as exc:
        # The navigation succeeded but its session ended before the labels were read; the
        # metadata watch labels whatever page a reopened pane shows.
        log.warning("browser pane %s navigated but could not read the page labels: %s", thread_id, exc)
        current_url, title = (str(url) if action is None else None), None
    return {"type": "navigate_result", "error": None, "url": current_url, "domain": url_domain(current_url), "title": title}
