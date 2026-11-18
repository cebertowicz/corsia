"""Back and forward on a session that held a page: load it fresh, or leave nothing readable."""
from __future__ import annotations

import asyncio
import json
import logging
from functools import partial
from typing import Any

from host_access import browser_cdp, browser_control
from host_access.browser_session_state import BrowserSessionError

# The page's own request on a reload, held so a form's POST is never sent again.
_DOCUMENT_REQUESTS = [{"urlPattern": "*", "resourceType": "Document", "requestStage": "Request"}]
RELOAD_SECONDS = 10.0
COMMIT_READS = 50
COMMIT_INTERVAL = 0.1


async def reopen_fresh(endpoint: str) -> None:
    """Load the page's own history entry again, as a new document, leaving history as it was.

    Nothing of the old entry is restored: not the back-forward cache's document, not form values. A navigation
    to the same address replaces the entry, so it is used where it starts a new document. An address with a
    #fragment would be a same-document navigation that keeps the restored page and adds an entry, so it is
    reloaded in place instead. A navigation that starts no new document or doesn't commit (errorText: a 204, a
    download) raises, so the caller blanks the page.
    """
    async with browser_cdp.CdpClient(endpoint) as cdp:
        history = await cdp.call("Page.getNavigationHistory")
        entries = history.get("entries")
        index = browser_cdp.protocol_int(history.get("currentIndex"))
        url = entries[index].get("url") if isinstance(entries, list) and 0 <= index < len(entries) else None
        if not isinstance(url, str) or not url:
            raise BrowserSessionError("This page couldn't be checked")
        if "#" in url:
            await _reload_sending_no_form(cdp)
            return
        result = await cdp.call("Page.navigate", url=url)
        if result.get("errorText") or not result.get("loaderId"):
            raise BrowserSessionError("This page couldn't be loaded again")


async def _reload_sending_no_form(cdp: browser_cdp.CdpClient) -> None:
    """Reload the current entry in place, never sending a form again; raise unless a new document commits.

    A reload repeats the request that made the entry, a form's POST included (Chrome's Page.reload asks no
    one), so the page's own request is held: a GET goes on, anything else is stopped and Chrome shows its
    blocked page in place of the entry. Other frames' document requests go on untouched.
    """
    frame = (await cdp.call("Page.getFrameTree")).get("frameTree", {}).get("frame", {})
    main, before = frame.get("id"), frame.get("loaderId")
    await cdp.call("Fetch.enable", patterns=_DOCUMENT_REQUESTS)
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + RELOAD_SECONDS
        reload_id = await cdp.send("Page.reload")
        acknowledged = decided = False
        while not (acknowledged and decided):
            try:
                message = json.loads(await asyncio.wait_for(cdp.socket.recv(), max(0.0, deadline - loop.time())))
            except TimeoutError as exc:
                raise BrowserSessionError("This page couldn't be loaded again") from exc
            if message.get("id") == reload_id:
                if "error" in message:
                    raise BrowserSessionError("This page couldn't be loaded again")
                acknowledged = True
            elif message.get("method") == "Fetch.requestPaused":
                params = message.get("params") or {}
                request_id = params.get("requestId")
                if params.get("frameId") == main and not decided:
                    decided = True
                    if (params.get("request") or {}).get("method") != "GET":
                        await cdp.send("Fetch.failRequest", requestId=request_id, errorReason="BlockedByClient")
                        continue
                await cdp.send("Fetch.continueRequest", requestId=request_id)
    finally:
        # Disabling lets any request still held go on, so no frame is left waiting.
        await cdp.call("Fetch.disable")
    for _ in range(COMMIT_READS):
        frame = (await cdp.call("Page.getFrameTree")).get("frameTree", {}).get("frame", {})
        if frame.get("loaderId") and frame.get("loaderId") != before:
            return
        await asyncio.sleep(COMMIT_INTERVAL)
    raise BrowserSessionError("This page couldn't be loaded again")


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
