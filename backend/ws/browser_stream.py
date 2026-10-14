"""Authenticated relay between the browser pane and agent-browser's loopback stream."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets

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
        async with websockets.connect(f"ws://127.0.0.1:{outcome.port}", max_size=None) as stream:
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
    """Proxy frames and accept input only from this connection's control holder."""
    connection_token = secrets.token_urlsafe(18)

    reliable: asyncio.Queue[str] = asyncio.Queue()
    latest_frame: str | None = None
    frame_ready = asyncio.Event()

    async def send_control() -> None:
        await reliable.put(json.dumps({
            "type": "control",
            "has_control": browser_control.holds_control(thread_id, connection_token),
        }))

    async def send_latest() -> None:
        """Keep control/status ordered while replacing frames a slow pane cannot yet see."""
        nonlocal latest_frame
        while True:
            try:
                message = reliable.get_nowait()
            except asyncio.QueueEmpty:
                message = None
            if message is not None:
                await pane.send_text(message)
                continue
            if latest_frame is not None:
                message, latest_frame = latest_frame, None
                frame_ready.clear()
                await pane.send_text(message)
                continue
            reliable_wait = asyncio.create_task(reliable.get())
            frame_wait = asyncio.create_task(frame_ready.wait())
            done, pending = await asyncio.wait({reliable_wait, frame_wait}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if reliable_wait in done:
                await pane.send_text(reliable_wait.result())

    async def from_stream() -> None:
        nonlocal latest_frame
        async for message in stream:
            try:
                kind = json.loads(message).get("type")
            except (TypeError, ValueError):
                kind = None
            if kind == "frame":
                latest_frame = message
                frame_ready.set()
            else:
                await reliable.put(message)

    async def from_pane() -> None:
        while True:
            message = await pane.receive_text()
            try:
                data = json.loads(message)
            except (TypeError, ValueError):
                continue
            kind = data.get("type") if isinstance(data, dict) else None
            if kind == "take_over":
                if browser_control.take_control(thread_id, connection_token):
                    emit_product_event("browser_pane", "take_over", thread_id=thread_id, user_id=user_id)
                await send_control()
            elif kind == "hand_back":
                if browser_control.hand_back(thread_id, connection_token):
                    emit_product_event("browser_pane", "hand_back", thread_id=thread_id, user_id=user_id)
                await send_control()
            elif kind == "navigate":
                # Navigation can take seconds; keep accepting control input while it runs.
                task = asyncio.create_task(_navigate(thread_id, connection_token, data))

                def publish_navigation(done: asyncio.Task) -> None:
                    if done.cancelled():
                        return
                    try:
                        result = done.result()
                    except Exception as exc:
                        result = {"type": "navigate_result", "error": str(exc)}
                    reliable.put_nowait(json.dumps(result))

                task.add_done_callback(publish_navigation)
            elif kind == "appearance":
                _request_match(thread_id, connection_token, data)
            elif (kind in {"input_mouse", "input_keyboard"}
                  and browser_control.holds_control(thread_id, connection_token)):
                await stream.send(message)

    try:
        # A stream connection alone supplies stale dimensions in agent-browser 0.31.2; this explicit
        # screencast keeps the remote viewport matched to the pane.
        await stream.send(json.dumps({"type": "screencast_start"}))
        await send_control()
        tasks = {asyncio.create_task(from_stream()), asyncio.create_task(from_pane()), asyncio.create_task(send_latest())}
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()
    finally:
        # A closed connection's size must never land after a newer pane's.
        if (queued := _latest_appearance.get(thread_id)) and queued[0] == connection_token:
            del _latest_appearance[thread_id]
        if browser_control.hand_back(thread_id, connection_token):
            emit_product_event("browser_pane", "hand_back", thread_id=thread_id, user_id=user_id)


# Matching waits behind any running page command, so it runs beside each input loop. Per thread,
# only the newest request from any pane connection is applied, one at a time. A match is never
# cancelled: cancelling a running browser command resets the session.
_latest_appearance: dict[str, tuple[str, browser_pane.PaneAppearance]] = {}
_matchers: dict[str, asyncio.Task] = {}


def _request_match(thread_id: str, connection_token: str, data: dict) -> None:
    if (appearance := browser_pane.PaneAppearance.parse(data)) is None:
        return
    _latest_appearance[thread_id] = (connection_token, appearance)
    if (matcher := _matchers.get(thread_id)) is None or matcher.done():
        matcher = _matchers[thread_id] = asyncio.create_task(_match_latest(thread_id))
        matcher.add_done_callback(lambda done: _matchers.pop(thread_id) if _matchers.get(thread_id) is done else None)


async def _match_latest(thread_id: str) -> None:
    """Match the page to the newest pane; a refusal keeps the old size, which the pane letterboxes."""
    while (queued := _latest_appearance.pop(thread_id, None)) is not None:
        connection_token, appearance = queued
        try:
            await browser_pane.match_pane(thread_id, appearance, connection_token)
            await browser_pane.repaint(thread_id, connection_token)
        except (BrowserSessionError, ProcessOwnerError) as exc:
            log.warning("browser pane %s kept its old appearance: %s", thread_id, exc)


async def _navigate(thread_id: str, connection_token: str, data: dict) -> dict:
    """Open a URL, or go back, forward or reload, only for the connection that holds control."""
    if not browser_control.holds_control(thread_id, connection_token):
        return {"type": "navigate_result", "error": "Take over to navigate"}
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
    return {"type": "navigate_result", "error": result if result.startswith("Error:") else None}
