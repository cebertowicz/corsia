"""Clear one site's browser data using the origins CDP can identify.

Closed origins are cleared when they were recorded or have a matching cookie.
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from host_access import browser_cdp
from host_access.browser_domain import registrable_domain, url_domain

# Chrome for Testing 152.0.7977.75, disposable profile: Storage.setCookies filled
# 25 domains x 180 cookies with 4,000-byte values. Chrome kept 3,296 plain cookies
# (its global limit), yielding a ~13.96 MB getCookies reply (~4,237 bytes/cookie).
# With 25 partitions added, 3,346 cookies (50 partitioned) yielded ~14.18 MB.
# 32 MiB covers the full plain jar plus roughly 4,000 full-size partitioned cookies.
SITE_CLEAR_MAX_MESSAGE = 32 * 1024 * 1024


async def _call(cdp, method, *, session=None, **params):
    return await asyncio.wait_for(cdp.call_in(session, method, **params), browser_cdp.BROWSER_CALL_TIMEOUT)


def _origin(url: object, domain: str) -> str | None:
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or url_domain(url) != domain:
            return None
        host = parts.hostname
        if ":" in host:
            host = f"[{host}]"
        port = f":{parts.port}" if parts.port is not None else ""
        return f"{parts.scheme}://{host}{port}"
    except ValueError:
        return None


def _frames(tree: object, domain: str) -> set[str]:
    if not isinstance(tree, dict) or not isinstance(tree.get("frame"), dict):
        raise RuntimeError("frame tree could not be checked")
    frame = tree["frame"]
    origins = {origin for value in (frame.get("url"), frame.get("securityOrigin"))
               if (origin := _origin(value, domain))}
    for child in tree.get("childFrames") or ():
        origins.update(_frames(child, domain))
    return origins


def cookie_matches(cookie: object, domain: str) -> bool:
    if not isinstance(cookie, dict) or not isinstance(cookie.get("domain"), str):
        return False
    host = cookie["domain"].lstrip(".").rstrip(".").lower()
    return bool(host) and registrable_domain(host) == domain


async def _close_site_targets(cdp, targets: list, domain: str, origins: set[str]) -> None:
    for target in targets:
        if not isinstance(target, dict) or target.get("type") != "page":
            continue
        target_id = target.get("targetId")
        if not isinstance(target_id, str):
            raise RuntimeError("page has no target id")
        session = (await _call(cdp, "Target.attachToTarget", targetId=target_id, flatten=True)).get("sessionId")
        if not isinstance(session, str):
            raise RuntimeError("page attachment failed")
        tree = (await _call(cdp, "Page.getFrameTree", session=session)).get("frameTree")
        seen = _frames(tree, domain)
        if origin := _origin(target.get("url"), domain):
            seen.add(origin)
        origins.update(seen)
        if seen:
            result = await _call(cdp, "Target.closeTarget", targetId=target_id)
            if result.get("success") is not True:
                raise RuntimeError("target close was not confirmed")


async def clear(endpoint: str, site: str, *, recorded_origins: tuple[str, ...] = ()) -> None:
    """Close matching targets, delete cookies, clear known origins and verify cookies.

    Confirmed means every named-origin clear succeeded and the cookie re-read is empty. Each delete and clear
    is awaited before the re-read is sent on the same connection, so the re-read needs no wait. An unrelated
    response can still set a cookie after that re-read.
    """
    domain = url_domain(site)
    if not domain:
        raise ValueError("site has no registrable domain")
    async with browser_cdp.CdpClient(endpoint, max_size=SITE_CLEAR_MAX_MESSAGE) as cdp:
        cookies = (await _call(cdp, "Storage.getCookies")).get("cookies")
        if not isinstance(cookies, list):
            raise RuntimeError("cookie read did not return a list")
        matched = [cookie for cookie in cookies if cookie_matches(cookie, domain)]
        hosts = {domain, *(cookie["domain"].lstrip(".").rstrip(".").lower() for cookie in matched)}
        origins = {f"{scheme}://{host}" for host in hosts for scheme in ("http", "https")}
        origins.update(origin for value in recorded_origins if (origin := _origin(value, domain)))
        if origin := _origin(site, domain):
            origins.add(origin)
        targets = (await _call(cdp, "Target.getTargets")).get("targetInfos")
        if not isinstance(targets, list):
            raise RuntimeError("target read did not return a list")
        # Keep Chrome alive when closing its last tab; Network.deleteCookies also needs a page session.
        target_id = (await _call(cdp, "Target.createTarget", url="about:blank")).get("targetId")
        if not isinstance(target_id, str):
            raise RuntimeError("clear target could not be created")
        try:
            await _close_site_targets(cdp, targets, domain, origins)
            session = (await _call(cdp, "Target.attachToTarget", targetId=target_id, flatten=True)).get("sessionId")
            if not isinstance(session, str):
                raise RuntimeError("clear target could not be attached")
            await _call(cdp, "Network.enable", session=session)
            for cookie in matched:
                params = {key: cookie[key] for key in ("name", "domain", "path", "partitionKey") if key in cookie}
                if not all(key in params for key in ("name", "domain", "path")):
                    raise RuntimeError("cookie identity incomplete")
                await _call(cdp, "Network.deleteCookies", session=session, **params)
            for origin in sorted(origins):
                await _call(cdp, "Storage.clearDataForOrigin", session=session, origin=origin, storageTypes="all")
            remaining = (await _call(cdp, "Storage.getCookies")).get("cookies")
            if not isinstance(remaining, list) or any(cookie_matches(cookie, domain) for cookie in remaining):
                raise RuntimeError("site cookies remain after clearing")
        finally:
            await _call(cdp, "Target.closeTarget", targetId=target_id)
