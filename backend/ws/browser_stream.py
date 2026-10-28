"""Authenticated relay between the browser pane and the shared page it shows."""
from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import secrets
from functools import partial
from typing import AsyncIterator, Awaitable, Callable

from fastapi import WebSocket, WebSocketDisconnect
from websockets.exceptions import WebSocketException

from host_access import browser_control, browser_pane, browser_pane_state
from host_access.browser_domain import url_domain
from host_access.browser_session_state import BrowserSessionError
from host_access.processes import ProcessOwnerError
from infra.auth import authenticate, narrow_to_listener
from infra.database import get_db
from infra.product_events import emit_product_event
from infra.queries import get_thread_for_user
from ws import browser_clipboard
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


async def _retry_pane_connection(thread_id: str, connection_token: str, kind: str,
                                 terminal_reason: str, connect: Callable[[], Awaitable[None]]) -> None:
    """Reconnect one CDP stream after transient loss, with one bounded policy for every stream."""
    failures = 0
    while True:
        started = asyncio.get_running_loop().time()
        clean_return = False
        try:
            await connect()
        except (BrowserSessionError, ProcessOwnerError, OSError, WebSocketException) as exc:
            error = exc
        else:
            # A clean return still means this stream ended; only a long run proves it was stable.
            clean_return = True
            error = BrowserSessionError(f"{kind} ended")
        if asyncio.get_running_loop().time() - started >= PANE_RETRY_RESET_SECONDS:
            failures = 0
        failures += 1
        log.warning("browser pane %s lost its %s (attempt %d of %d): %s",
                    thread_id, kind, failures, len(PANE_RETRY_DELAYS) + 1, error)
        if failures > len(PANE_RETRY_DELAYS):
            if not clean_return and isinstance(error, BrowserSessionError):
                raise error
            raise BrowserSessionError(terminal_reason) from error
        browser_pane.clear_page_endpoint(thread_id, connection_token)
        await asyncio.sleep(PANE_RETRY_DELAYS[failures - 1])


async def _screencast_events(thread_id: str, connection_token: str, frames: PaneFrames) -> None:
    """Reconnect the pane frame stream after a transient CDP failure."""
    def deliver(frame: str) -> object:
        return frames.offer(frame)

    await _retry_pane_connection(
        thread_id, connection_token, "screencast", "The pane stopped showing the page",
        lambda: browser_pane.screencast(thread_id, connection_token, deliver),
    )


async def _metadata_events(thread_id: str, connection_token: str, report: Callable[[dict], None]) -> None:
    """Reconnect page-address metadata while the pane stays connected."""
    async def emit(message: dict) -> None:
        report(message)

    await _retry_pane_connection(
        thread_id, connection_token, "page address watch", "The pane stopped following the page's address",
        lambda: browser_pane.watch_page_metadata(thread_id, connection_token, emit),
    )


async def _watch_targets(thread_id: str, connection_token: str, changed: asyncio.Event,
                         pages: list[set[str] | None]) -> None:
    """Report tab changes; once retries run out the pane keeps showing its current tab."""
    try:
        await _retry_pane_connection(
            thread_id, connection_token, "target watch", "target watch ended",
            lambda: browser_pane.watch_targets(thread_id, changed.set, pages),
        )
    except BrowserSessionError as exc:
        # Agent tab switches still reach the pane through target_changed; only browser-opened tabs go unfollowed.
        log.warning("browser pane %s stopped watching tabs and keeps its current tab: %s", thread_id, exc)
    except Exception:
        log.exception("browser pane %s tab watch failed; it keeps its current tab", thread_id)


async def _follow_page(thread_id: str, connection_token: str, frames: PaneFrames,
                       report: Callable[[dict], None]) -> None:
    """Restart both page streams on a target change without reconnecting the pane."""
    changed = asyncio.Event()
    browser_pane_state.watch_target_changes(thread_id, connection_token, changed.set)
    pages: list[set[str] | None] = [None]
    watcher = asyncio.create_task(_watch_targets(thread_id, connection_token, changed, pages))
    streams: set[asyncio.Task] = set()
    try:
        while True:
            change_wait = asyncio.create_task(changed.wait())
            streams = {
                asyncio.create_task(_screencast_events(thread_id, connection_token, frames)),
                asyncio.create_task(_metadata_events(thread_id, connection_token, report)),
                change_wait,
            }
            while True:
                waiting = streams if watcher.done() else streams | {watcher}
                done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                if done != {watcher}:
                    break
            for task in done:
                if task is change_wait or task is watcher:
                    continue
                task.result()
            changed.clear()
            await asyncio.to_thread(emit_product_event, "browser_pane", "target_changed", thread_id=thread_id)
            for task in streams:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*streams, return_exceptions=True)
            browser_pane.clear_page_endpoint(thread_id, connection_token)
            report({"type": "browser_target_changed"})  # Device metrics belong to each page target.
    finally:
        browser_pane_state.unwatch_target_changes(thread_id, connection_token)
        watcher.cancel()
        for task in streams:
            if not task.done():
                task.cancel()
        await asyncio.gather(watcher, *streams, return_exceptions=True)


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
                                   partial(browser_pane.password_probe, thread_id, connection_token),
                                   session=partial(browser_pane.session_identity, thread_id))

    async def from_pane() -> None:
        async with contextlib.aclosing(_pane_messages(pane)) as messages:
            async for data in messages:
                kind = data.get("type")
                if kind == "navigate":
                    _request_navigate(thread_id, connection_token, data, report)
                elif kind == "appearance":
                    _request_match(thread_id, connection_token, data, report)
                elif kind == "input_mouse":
                    # CDP serializes pointer and key delivery on the page target, so typing cannot
                    # overtake the click that focuses its field.
                    await browser_pane.mouse_event(thread_id, data, connection_token)
                    browser_control.person_input(thread_id)
                elif kind == "input_keyboard":
                    await browser_pane.key_event(thread_id, data, connection_token)
                    browser_control.person_input(thread_id)
                elif kind in {"clipboard_selection", "clipboard_paste", "clipboard_cut"}:
                    await browser_clipboard.handle(thread_id, connection_token, data, report)

    tasks: set[asyncio.Task] = set()
    try:
        # Shared panes have no control-holder state.
        tasks = {
            asyncio.create_task(_follow_page(thread_id, connection_token, frames, report)),
            asyncio.create_task(from_pane()),
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


async def _pane_messages(pane: WebSocket) -> AsyncIterator[dict]:
    """The pane's messages in the order sent, less each pointer move a later queued move replaces.

    Chrome answers a pointer move only after its next frame, tens of milliseconds, while the pane
    sends one per animation frame; delivered one at a time, moves pile up and a click behind them
    lands seconds late. So the pane is read while earlier messages are delivered, and a move that
    arrives right behind a queued move replaces it, so a stalled page never holds more than one
    waiting move. Nothing else is dropped or reordered, and the pane's disconnect is raised only
    after everything it sent first. A page that answers no input while the backlog reaches
    `_MAX_QUEUED_INPUT` is stalled: the pane is closed, as an overflowing output path closes it.
    """
    received: collections.deque[dict | Exception] = collections.deque()
    arrived = asyncio.Event()

    reader = asyncio.create_task(_receive_pane_messages(pane, received, arrived))
    try:
        while True:
            await arrived.wait()
            arrived.clear()
            while received:
                data = received.popleft()
                if isinstance(data, Exception):
                    raise data
                yield data
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


async def _receive_pane_messages(pane: WebSocket, received: collections.deque[dict | Exception],
                                 arrived: asyncio.Event) -> None:
    try:
        while True:
            try:
                data = json.loads(await pane.receive_text())
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict):
                if _is_move(data) and received and _is_move(received[-1]):
                    received[-1] = data
                elif len(received) >= _MAX_QUEUED_INPUT:
                    received.clear()
                    with contextlib.suppress(Exception):
                        await pane.close(code=1013)
                    raise WebSocketDisconnect(code=1013)
                else:
                    received.append(data)
                arrived.set()
    except Exception as exc:
        received.append(exc)
        arrived.set()


# The pane sends at most one wheel event per animation frame, so this is about 17 s of input
# against a page that has answered none of it.
_MAX_QUEUED_INPUT = 1024


def _is_move(data: dict | Exception) -> bool:
    return isinstance(data, dict) and data.get("type") == "input_mouse" and data.get("eventType") == "mouseMoved"


async def _close_on_overflow(pane: WebSocket, overflow: asyncio.Event) -> None:
    """Close the pane once any reliable output path exhausts its bounded queue."""
    await overflow.wait()
    with contextlib.suppress(Exception):
        await pane.close(code=1013)


# Page commands wait behind any running browser command, so one runner per thread runs them beside
# every input loop. Only the newest appearance from any pane connection is applied, and at most one
# navigation waits: a newer one replaces it. A running command is never cancelled: cancelling it
# resets the shared session. A navigation carries its own pane's report, so no relay owns the runner.
_latest_appearance: dict[str, tuple[str, browser_pane.PaneAppearance, Callable[[dict], None]]] = {}
_pending_navigation: dict[str, tuple[str, dict, Callable[[dict], None]]] = {}
_runners: dict[str, asyncio.Task] = {}


def _request_match(thread_id: str, connection_token: str, data: dict, report: Callable[[dict], None]) -> None:
    if (appearance := browser_pane.PaneAppearance.parse(data)) is None:
        return
    _latest_appearance[thread_id] = (connection_token, appearance, report)
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
            connection_token, appearance, report = queued
            try:
                await browser_pane.match_pane(thread_id, appearance)
            except (BrowserSessionError, ProcessOwnerError) as exc:
                log.warning("browser pane %s kept its old appearance: %s", thread_id, exc)
                report({"type": "pane_error", "error": "CORSIA couldn't fit the page to the pane. Try resizing the pane again."})
        else:
            return


async def _navigate(thread_id: str, connection_token: str, data: dict) -> dict:
    """Open a URL, or go back, forward or reload from an authenticated pane connection."""
    action, url = data.get("action"), data.get("url")
    if action is None:
        if (url := browser_control.normalize_browser_url(url)) is None:
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
