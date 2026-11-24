"""Read a running browser's sign-in for a copy, without opening a tab or loading a page in it.

The result has agent-browser's state-file shape (``{"cookies": [...], "origins": [...]}``), which the copy
restores with ``state load``. Cookies come from the whole browser; page storage only from the tabs already
open, read in place. agent-browser's own ``state save`` instead loads each origin the session visited in a
new tab of the holder's browser, and can stall there for its whole timeout.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from host_access import browser_cdp

_COOKIE_TIMEOUT = 5.0
_PAGE_TIMEOUT = 3.0
_READ_STORAGE = """(() => {
  const items = (store) => { const out = [];
    for (let i = 0; i < store.length; i++) { const key = store.key(i); out.push({name: key, value: store.getItem(key)}); }
    return out; };
  return JSON.stringify({origin: location.origin, localStorage: items(localStorage), sessionStorage: items(sessionStorage)});
})()"""


async def read(endpoint: str) -> dict[str, list[dict[str, Any]]]:
    """The browser's cookies, and the storage of each open web page; a page that doesn't answer is left out."""
    async with browser_cdp.CdpClient(endpoint, max_size=None) as cdp:
        cookies = (await asyncio.wait_for(cdp.call("Storage.getCookies"), _COOKIE_TIMEOUT)).get("cookies")
        targets = (await asyncio.wait_for(cdp.call("Target.getTargets"), _COOKIE_TIMEOUT)).get("targetInfos")
        origins: dict[str, dict[str, Any]] = {}
        for target in targets or ():
            if not isinstance(target, dict) or target.get("type") != "page":
                continue
            if not str(target.get("url", "")).startswith(("http://", "https://")):
                continue
            try:
                storage = await asyncio.wait_for(_page_storage(cdp, str(target.get("targetId"))), _PAGE_TIMEOUT)
            except Exception:
                # A page busy or closing leaves only its own storage out; a late answer to it carries an
                # older request id, which later reads on this connection skip.
                continue
            if storage is not None:
                origins[storage["origin"]] = storage
    return {"cookies": [cookie for cookie in cookies or () if isinstance(cookie, dict)],
            "origins": list(origins.values())}


async def _page_storage(cdp: browser_cdp.CdpClient, target_id: str) -> dict[str, Any] | None:
    session = (await cdp.call("Target.attachToTarget", targetId=target_id, flatten=True)).get("sessionId")
    if not isinstance(session, str):
        return None
    # Closing this connection detaches the session; a detach here could itself wait on a stuck page.
    result = await cdp.call_in(session, "Runtime.evaluate", expression=_READ_STORAGE, returnByValue=True)
    value = result.get("result", {}).get("value") if "exceptionDetails" not in result else None
    storage = json.loads(value) if isinstance(value, str) else None
    if not isinstance(storage, dict) or not str(storage.get("origin", "")).startswith(("http://", "https://")):
        return None
    if not storage.get("localStorage") and not storage.get("sessionStorage"):
        return None
    return storage
