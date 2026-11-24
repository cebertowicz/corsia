"""Read a running browser's sign-in for a copy, without opening a tab or loading a page in it.

The result has agent-browser's state-file shape (``{"cookies": [...], "origins": [...]}``), which the copy
restores with ``state load``. Cookies come from the whole browser; page storage only from the tabs already
open, read in place. agent-browser's own ``state save`` instead loads each origin the session visited in a
new tab of the holder's browser, and can stall there for its whole timeout.

Each tab's storage is read through the DevTools storage domain under the origin the browser reports for
that tab, never by running script in the page, so a page can't choose which site's storage it fills.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit

from host_access import browser_cdp
from host_access.browser_session_state import BrowserSessionError

log = logging.getLogger(__name__)
_COOKIE_TIMEOUT = 5.0
# A live tab answers a storage read in milliseconds; a stuck one (a busy script, an open dialog) never does.
# 2 s is far above a live answer on a loaded machine, so only a stuck tab is left out.
_PAGE_TIMEOUT = 2.0
# Tabs are read one after another and share this budget, so the whole read ends inside the copy's own
# limit (browser_session_launch._COPY_READ_TIMEOUT, 30 s) and the cookies already read are always kept.
_PAGES_BUDGET = 12.0


async def read(endpoint: str) -> dict[str, list[dict[str, Any]]]:
    """The browser's cookies, and the storage of each open web page; a page that doesn't answer is left out."""
    async with browser_cdp.CdpClient(endpoint, max_size=None) as cdp:
        cookies = (await asyncio.wait_for(cdp.call("Storage.getCookies"), _COOKIE_TIMEOUT)).get("cookies")
        targets = (await asyncio.wait_for(cdp.call("Target.getTargets"), _COOKIE_TIMEOUT)).get("targetInfos")
        pages = [(str(target.get("targetId")), origin) for target in targets or ()
                 if (origin := _page_origin(target)) is not None]
        origins: dict[str, dict[str, Any]] = {}
        deadline = asyncio.get_running_loop().time() + _PAGES_BUDGET
        for index, (target_id, origin) in enumerate(pages):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                log.info("browser sign-in copy left out the storage of %d open pages: out of time", len(pages) - index)
                break
            try:
                # A late answer to a stuck page carries an older request id, which later reads here skip.
                storage = await asyncio.wait_for(_page_storage(cdp, target_id, origin), min(_PAGE_TIMEOUT, remaining))
            except (TimeoutError, BrowserSessionError) as exc:
                log.info("browser sign-in copy left out one page's storage: %s", type(exc).__name__)
                continue
            if storage is not None:
                origins[origin] = storage
    return {"cookies": [cookie for cookie in cookies or () if isinstance(cookie, dict)],
            "origins": list(origins.values())}


def _page_origin(target: object) -> str | None:
    """The web origin the browser reports for a page target; None for anything else."""
    if not isinstance(target, dict) or target.get("type") != "page":
        return None
    parts = urlsplit(str(target.get("url", "")))
    host = parts.netloc.rpartition("@")[2]
    if parts.scheme not in ("http", "https") or not host:
        return None
    return f"{parts.scheme}://{host}"


async def _page_storage(cdp: browser_cdp.CdpClient, target_id: str, origin: str) -> dict[str, Any] | None:
    session = (await cdp.call("Target.attachToTarget", targetId=target_id, flatten=True)).get("sessionId")
    if not isinstance(session, str):
        return None
    # Closing this connection detaches the session; a detach here could itself wait on a stuck page.
    local = await _items(cdp, session, origin, local=True)
    per_tab = await _items(cdp, session, origin, local=False)
    if not local and not per_tab:
        return None
    return {"origin": origin, "localStorage": local, "sessionStorage": per_tab}


async def _items(cdp: browser_cdp.CdpClient, session: str, origin: str, *, local: bool) -> list[dict[str, str]]:
    result = await cdp.call_in(session, "DOMStorage.getDOMStorageItems",
                               storageId={"securityOrigin": origin, "isLocalStorage": local})
    return [{"name": entry[0], "value": entry[1]} for entry in result.get("entries") or ()
            if isinstance(entry, list) and len(entry) == 2 and all(isinstance(part, str) for part in entry)]
