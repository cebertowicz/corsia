"""Relay explicit person clipboard actions to the pane's shared page."""
import asyncio
from typing import Callable

from websockets.exceptions import WebSocketException

from host_access import browser_control, browser_pane
from host_access.browser_clipboard import PageHandledError
from host_access.browser_session_state import BrowserSessionError
from infra.product_events import emit_product_event


async def handle(thread_id: str, connection: str, data: dict, report: Callable[[dict], None]) -> None:
    kind = data.get("type")
    if kind == "clipboard_selection" and isinstance(data.get("id"), int):
        cut = data.get("cut") is True
        action = "cut" if cut else "copy"
        outcome = "failed"
        try:
            text = await browser_pane.clipboard_selection(thread_id, connection, cut)
            outcome = "captured" if text else "empty"
            report({"type": kind, "id": data["id"], "text": text})
        except PageHandledError:
            outcome = "page_handled"
            report({"type": kind, "id": data["id"], "error": "This page handles copying itself, so CORSIA couldn’t copy the text."})
        except (BrowserSessionError, OSError, WebSocketException, TimeoutError):
            report({"type": kind, "id": data["id"], "error": "CORSIA couldn’t copy from this page. Try copying again."})
        await asyncio.to_thread(emit_product_event, "browser_pane", "clipboard", thread_id=thread_id,
                                properties={"action": action, "outcome": outcome})
    elif kind == "clipboard_paste" and isinstance(data.get("text"), str):
        outcome = "failed"
        try:
            await browser_pane.clipboard_paste(thread_id, connection, data["text"])
            browser_control.person_input(thread_id)
            outcome = "ok"
            report({"type": "pane_error", "source": "clipboard", "error": None})
        except (BrowserSessionError, OSError, WebSocketException, TimeoutError):
            report({"type": "pane_error", "source": "clipboard", "error": "CORSIA couldn’t paste into the page. Try pasting again."})
        await asyncio.to_thread(emit_product_event, "browser_pane", "clipboard", thread_id=thread_id,
                                properties={"action": "paste", "outcome": outcome})
    elif kind == "clipboard_undo":
        try:
            await browser_pane.clipboard_undo(thread_id, connection)
            report({"type": "pane_error", "source": "clipboard", "error": None})
        except (BrowserSessionError, OSError, WebSocketException, TimeoutError):
            report({"type": "pane_error", "source": "clipboard", "error": "CORSIA couldn’t undo the cut. Try undoing in the page."})
