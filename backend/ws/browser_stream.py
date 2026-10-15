"""Authenticated relay between the browser pane and agent-browser's loopback stream."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
from typing import Callable

import websockets
from fastapi import WebSocket, WebSocketDisconnect
from websockets.exceptions import WebSocketException

from host_access import browser_control, browser_pane
from host_access.browser_session_state import BrowserSessionError
from host_access.processes import ProcessOwnerError
from infra.auth import authenticate
from infra.database import get_db
from infra.product_events import emit_product_event
from infra.queries import get_thread_for_user
from ws.browser_frames import PaneFrames

log = logging.getLogger(__name__)


async def browser_stream_endpoint(ws: WebSocket, thread_id: str) -> None:
    """Relay one owned thread's private stream without joining chat socket state."""
    await ws.accept()
    user = await _authenticate_pane_stream(ws)
    if user is None:
        return
    if not _user_owns_thread(thread_id, user["id"]):
        await ws.close(code=1008)
        return
    outcome = await browser_pane.stream_port(thread_id)
    if outcome.no_session:
        await ws.send_json({"type": "browser_state", "state": "no_browser_yet"})
        await ws.close()
        return
    if outcome.error:
        await ws.send_json({"type": "browser_state", "state": "error", "reason": outcome.error})
        await ws.close()
        return
    try:
        async with websockets.connect(f"ws://127.0.0.1:{outcome.port}", max_size=None, close_timeout=1) as stream:
            await _relay(ws, stream, thread_id, user["id"])
    except WebSocketDisconnect:
        pass
    except (OSError, WebSocketException):
        # The pane may already be gone; its own close handler then clears its control state.
        with contextlib.suppress(Exception):
            await ws.send_json({"type": "browser_state", "state": "error", "reason": "browser stream connection failed"})
            await ws.close()


async def _authenticate_pane_stream(ws: WebSocket) -> dict | None:
    """Authenticate the first pane message without chat presence registration."""
    try:
        message = await asyncio.wait_for(ws.receive_json(), timeout=10)
        token = message.get("token", "") if isinstance(message, dict) else ""
        user = authenticate(token)
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


async def _relay(pane: WebSocket, stream, thread_id: str, user_id: str) -> None:
    """Proxy shared page frames and pane input for one authenticated connection."""
    connection_token = secrets.token_urlsafe(18)

    frames = PaneFrames()
    overflow = asyncio.Event()

    def report(message: dict) -> None:
        # A result landing after this relay exits fills a queue nobody sends; that is harmless.
        if not frames.control(message):
            overflow.set()

    browser_control.pane_connected(thread_id, connection_token, report)

    async def from_stream() -> None:
        async for message in stream:
            if not frames.offer(message):
                overflow.set()
                return

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
            elif kind == "input_keyboard":
                await browser_pane.key_event(thread_id, data, connection_token)

    async def metadata_events() -> None:
        async def emit(message: dict) -> None:
            report(message)
        try:
            await browser_pane.watch_page_metadata(thread_id, connection_token, emit)
        except (BrowserSessionError, ProcessOwnerError):
            log.debug("browser pane metadata relay ended", exc_info=True)
            await asyncio.Future()

    tasks: set[asyncio.Task] = set()
    try:
        await stream.send(json.dumps({"type": "screencast_start"}))
        # Shared panes have no control-holder state.
        tasks = {
            asyncio.create_task(from_stream()),
            asyncio.create_task(from_pane()),
            asyncio.create_task(metadata_events()),
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
                await browser_pane.match_pane(thread_id, appearance, connection_token)
                await browser_pane.repaint(thread_id, connection_token)
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
            result = await browser_pane.go(thread_id, action, connection_token)
    except (BrowserSessionError, ProcessOwnerError) as exc:
        return {"type": "navigate_result", "error": str(exc)}
    if result.startswith("Error:"):
        return {"type": "navigate_result", "error": result}
    try:
        current_url, title = await browser_pane.page_info(thread_id, connection_token)
    except (BrowserSessionError, ProcessOwnerError):
        current_url, title = (str(url) if action is None else None), None
    return {"type": "navigate_result", "error": None, "url": current_url, "title": title}
