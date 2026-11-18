"""Back and forward on a session that held a page: load it fresh, or leave nothing readable."""
from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any

from host_access import browser_cdp, browser_control
from host_access.browser_session_state import BrowserSessionError


async def reopen_fresh(endpoint: str) -> None:
    """Navigate the page to its own address again, replacing its history entry.

    Nothing of the old entry is restored: not the back-forward cache's document, not form values.
    """
    async with browser_cdp.CdpClient(endpoint) as cdp:
        history = await cdp.call("Page.getNavigationHistory")
        entries = history.get("entries")
        index = browser_cdp.protocol_int(history.get("currentIndex"))
        url = entries[index].get("url") if isinstance(entries, list) and 0 <= index < len(entries) else None
        if not isinstance(url, str) or not url:
            raise BrowserSessionError("This page couldn't be checked")
        await cdp.call("Page.navigate", url=url)


async def fresh_after_history(owner: Any, runtime: Any, thread_id: str, command: Any) -> None:
    """Load a held page again after back or forward; if that can't be done, leave nothing readable.

    A restored page keeps its document and filled values, so a failed check or reload blanks the page (and,
    when even that fails, closes the session) before the error reaches the agent.
    """
    try:
        endpoint = await browser_cdp.page_endpoint(command)
        await browser_control.reopen_held_page(
            thread_id, partial(browser_cdp.page_password_probe, endpoint), partial(reopen_fresh, endpoint))
    except BaseException:
        try:
            await command(("open", "about:blank"), 10)
        except BaseException as blank_error:
            # A cancel while blanking still closes the session; the cancel itself then propagates.
            logging.getLogger(__name__).warning("browser page could not be blanked after history", exc_info=True)
            await owner._close_one(runtime)
            if isinstance(blank_error, asyncio.CancelledError):
                raise
        raise
