"""Relay explicit person clipboard actions to the pane's shared page."""
from typing import Callable

from host_access import browser_control, browser_pane
from host_access.browser_session_state import BrowserSessionError


async def handle(thread_id: str, connection: str, data: dict, report: Callable[[dict], None]) -> None:
    kind = data.get("type")
    if kind == "clipboard_selection" and isinstance(data.get("id"), int):
        try:
            text = await browser_pane.clipboard_selection(thread_id, connection)
            report({"type": kind, "id": data["id"], "text": text})
        except BrowserSessionError:
            report({"type": kind, "id": data["id"], "error": "Could not copy from the page"})
    elif kind in {"clipboard_paste", "clipboard_cut"} and isinstance(data.get("text"), str):
        try:
            action = browser_pane.clipboard_paste if kind == "clipboard_paste" else browser_pane.clipboard_cut
            await action(thread_id, connection, data["text"])
            browser_control.person_input(thread_id)
        except BrowserSessionError:
            report({"type": "clipboard_error", "error": "Could not update the page"})
