"""Relay explicit person clipboard actions to the pane's shared page."""
from typing import Callable

from websockets.exceptions import WebSocketException

from host_access import browser_control, browser_pane
from host_access.browser_session_state import BrowserSessionError


async def handle(thread_id: str, connection: str, data: dict, report: Callable[[dict], None]) -> None:
    kind = data.get("type")
    if kind == "clipboard_selection" and isinstance(data.get("id"), int):
        try:
            text = await browser_pane.clipboard_selection(thread_id, connection, data["id"])
            report({"type": kind, "id": data["id"], "text": text})
        except (BrowserSessionError, OSError, WebSocketException, TimeoutError):
            report({"type": kind, "id": data["id"], "error": "CORSIA couldn’t copy from the page. Select the text again and copy."})
    elif (kind == "clipboard_paste" or (kind == "clipboard_cut" and isinstance(data.get("id"), int))) and isinstance(data.get("text"), str):
        try:
            if kind == "clipboard_paste":
                await browser_pane.clipboard_paste(thread_id, connection, data["text"])
            else:
                await browser_pane.clipboard_cut(thread_id, connection, data["text"], data["id"])
            browser_control.person_input(thread_id)
            report({"type": "pane_error", "error": None})
        except (BrowserSessionError, OSError, WebSocketException, TimeoutError):
            message = ("CORSIA couldn’t cut the text. Select it again and cut."
                       if kind == "clipboard_cut" else "CORSIA couldn’t paste into the page. Try pasting again.")
            report({"type": "pane_error", "error": message})
