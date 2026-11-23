"""Back and forward on a session that held a page: load it fresh, or leave nothing readable."""
from __future__ import annotations

import asyncio
import json
import logging
from functools import partial
from typing import Any

from host_access import browser_cdp, browser_control
from host_access.browser_session_state import BrowserSessionError

# Every frame's document request on a reload, held so a form's POST is never sent again.
_DOCUMENT_REQUESTS = [{"urlPattern": "*", "resourceType": "Document", "requestStage": "Request"}]
# Bounds, measured on Chrome for Testing 152 with the product's chrome_argv (3 runs of 5 cases, Nov 23 2026: a
# reload after a frame POST and after a top-level POST, a page whose frame takes 4 s, and both POST pages with
# a zero deadline). Together they keep the worst
# case of a failed history move (10 + 5 + 50 x 0.1 + BLANK_SECONDS 10 = 30 s) within one more agent command
# at its default timeout (browser_runtime._DEFAULT_TIMEOUT, 30 s).
# A reload that hasn't loaded by then is stopped, not failed: whether a new document committed decides. Measured
# loads took 0.01-0.04 s; a frame still loading at the deadline was stopped and its page still committed.
# A navigation to the entry's address answers once it commits, before its load, so the same window bounds it;
# one that hasn't committed by then fails and the page is blanked (10 + BLANK_SECONDS 10 = 20 s at worst).
RELOAD_SECONDS = 10.0
# Chrome answered the stop in 0.001-0.003 s in every stopped case; no answer fails closed with requests held.
STOP_SECONDS = 5.0
# A committed reload showed its new loader id on the first read in every case; a load stopped before any
# document committed read all 50 and failed closed, so this window only bounds how long that failure takes.
COMMIT_READS = 50
COMMIT_INTERVAL = 0.1
# The rest of the 30-second budget above, for the blanking command after a failed reload.
BLANK_SECONDS = 10


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
        try:
            result = await asyncio.wait_for(cdp.call("Page.navigate", url=url), RELOAD_SECONDS)
        except TimeoutError as exc:
            raise BrowserSessionError("This page couldn't be loaded again") from exc
        if result.get("errorText") or not result.get("loaderId"):
            raise BrowserSessionError("This page couldn't be loaded again")


async def _reload_sending_no_form(cdp: browser_cdp.CdpClient) -> None:
    """Reload the current entry in place, never sending a form again; raise unless a new document commits.

    A reload repeats the requests that made the entry and its frames, a form's POST included (Chrome's
    Page.reload asks no one), so every document request is held until the page has loaded: a GET goes on,
    anything else is stopped and Chrome shows its blocked page in that frame's place. The load event comes only
    after every frame has loaded, so no frame's request is replayed unheld.
    """
    before = (await cdp.call("Page.getFrameTree")).get("frameTree", {}).get("frame", {}).get("loaderId")
    await cdp.call("Page.enable")
    await cdp.call("Fetch.enable", patterns=_DOCUMENT_REQUESTS)
    loaded = False
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + RELOAD_SECONDS
        reload_id = await cdp.send("Page.reload")
        acknowledged = False
        while not (acknowledged and loaded):
            try:
                message = json.loads(await asyncio.wait_for(cdp.socket.recv(), max(0.0, deadline - loop.time())))
            except TimeoutError:
                # Still loading: stopped below; whether a new document committed decides the result.
                break
            if message.get("id") == reload_id:
                if "error" in message:
                    raise BrowserSessionError("This page couldn't be loaded again")
                acknowledged = True
            elif message.get("method") == "Page.loadEventFired":
                loaded = True
            else:
                await _decide(cdp, message, get_goes_on=True)
    finally:
        await _release(cdp, loaded)
    for _ in range(COMMIT_READS):
        frame = (await cdp.call("Page.getFrameTree")).get("frameTree", {}).get("frame", {})
        if frame.get("loaderId") and frame.get("loaderId") != before:
            return
        await asyncio.sleep(COMMIT_INTERVAL)
    raise BrowserSessionError("This page couldn't be loaded again")


async def _decide(cdp: browser_cdp.CdpClient, message: dict, *, get_goes_on: bool) -> None:
    """A held document request goes on only if it is a GET and ``get_goes_on``; anything else is stopped."""
    if message.get("method") != "Fetch.requestPaused":
        return
    params = message.get("params") or {}
    if get_goes_on and (params.get("request") or {}).get("method") == "GET":
        await cdp.send("Fetch.continueRequest", requestId=params.get("requestId"))
    else:
        await cdp.send("Fetch.failRequest", requestId=params.get("requestId"), errorReason="BlockedByClient")


async def _release(cdp: browser_cdp.CdpClient, loaded: bool) -> None:
    """Stop holding document requests without letting a held one go out unchecked.

    Fetch.disable lets every request still held go on. Before the page has loaded, one may be held whose pause
    is still unread, so the load is stopped first and every request paused up to Chrome's answer is stopped too
    (Chrome sends events before the answer to a later command). A stopped load starts no new request. If Chrome
    doesn't answer, this raises with requests still held rather than letting them go.
    """
    if not loaded:
        stop_id = await cdp.send("Page.stopLoading")
        while True:
            try:
                message = json.loads(await asyncio.wait_for(cdp.socket.recv(), STOP_SECONDS))
            except TimeoutError as exc:
                raise BrowserSessionError("This page couldn't be loaded again") from exc
            if message.get("id") == stop_id:
                break
            await _decide(cdp, message, get_goes_on=False)
    await cdp.call("Fetch.disable")


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
            await command(("open", "about:blank"), BLANK_SECONDS)
        except BaseException as blank_error:
            # A cancel while blanking still closes the session; the cancel itself then propagates.
            logging.getLogger(__name__).warning("browser page could not be blanked after history", exc_info=True)
            if (close_error := await owner._close_one(runtime)) is not None:
                # The close already recorded its failure (browser_sessions._close_runtime): the session stays
                # blocked and a notice is filed. The restored page may still be open, so it is logged as an error.
                logging.getLogger(__name__).error("browser session could not be closed after history; its page may "
                                                  "still be open", exc_info=close_error)
            if isinstance(blank_error, asyncio.CancelledError):
                raise
        raise
