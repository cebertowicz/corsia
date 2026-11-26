"""Fence browser sessions while a granted site's sign-in is cleared."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from host_access import browser_grant_records as records, browser_launch, browser_site_clear
from host_access.browser_domain import url_domain
from host_access.browser_session_state import COMMAND_TIMEOUT

log = logging.getLogger(__name__)


class PendingClearError(RuntimeError):
    """A pending site's launch-time clear did not complete."""


async def _profile(runtime: Any) -> str | None:
    if runtime.record.profile:
        return runtime.record.profile
    if runtime.record.copy_of:
        value = await asyncio.to_thread(browser_launch.profile_dir, runtime.record.thread_id)
        return str(value) if value else None
    return None


def _drop_kept(profile: str, domain: str) -> None:
    from host_access import browser_grant_copy, browser_session_launch as launch
    with records._lock:
        path = launch.kept_signins_path(profile)
        launch._write_cookies(path, [cookie for cookie in browser_grant_copy.read_kept(path)
                                    if not browser_site_clear.cookie_matches(cookie, domain)])


async def _clear_site(endpoint: str, site: str, origins: tuple[str, ...]) -> None:
    if origins:
        await browser_site_clear.clear(endpoint, site, recorded_origins=origins)
    else:
        await browser_site_clear.clear(endpoint, site)


async def recover_launch(runtime: Any) -> None:
    """Clear journalled sites before a saved-profile launch serves any command."""
    profile = runtime.record.profile
    if not profile:
        return
    try:
        for domain, (sites, generation) in records.pending(profile).items():
            await asyncio.to_thread(_drop_kept, profile, domain)
            known = tuple(records.origins(profile, domain))
            for site in sites:
                await _clear_site(runtime.chrome.endpoint, site, known)
            if not records.complete(profile, domain, generation):
                raise RuntimeError("site clear superseded")
    except Exception as exc:
        log.warning("site clear at launch failed (%s)", type(exc).__name__)
        raise PendingClearError("browser session unavailable") from exc


async def _acquire(lock: asyncio.Lock) -> None:
    await asyncio.wait_for(lock.acquire(), COMMAND_TIMEOUT)


async def _clear_runtime(runtime: Any, sites: list[str], origins: tuple[str, ...]) -> bool:
    from host_access.browser_session_commands import require_settled
    await _acquire(runtime.action_lock)
    try:
        await _acquire(runtime.command_lock)
        try:
            require_settled(runtime)
            if runtime.chrome is None:
                from host_access.browser_session_launch import drop_copy
                drop_copy(runtime.record)
                return False
            if not runtime.chrome.endpoint:
                raise RuntimeError("browser endpoint unavailable")
            for site in sites:
                await _clear_site(runtime.chrome.endpoint, site, origins)
            return True
        finally:
            runtime.command_lock.release()
    finally:
        runtime.action_lock.release()


async def _close_failed(owner: Any, runtime: Any) -> bool:
    try:
        result = await asyncio.wait_for(owner._close_one(runtime), COMMAND_TIMEOUT)
        if result is None:
            return True
    except (Exception, asyncio.CancelledError):
        pass
    try:
        if runtime.chrome is not None:
            await asyncio.wait_for(runtime.chrome.close(), COMMAND_TIMEOUT)
            return await asyncio.wait_for(owner._close_one(runtime), COMMAND_TIMEOUT) is None
    except Exception:
        pass
    return False


async def clear_running(owner: Any, profile: str, domain: str) -> None:
    """Serialize site clears on the owner before touching session locks."""
    async with owner._grant_cleanup_lock:
        try:
            await _clear_running(owner, profile, domain)
        except Exception as exc:
            log.warning("site clear failed for %s (%s)", domain, type(exc).__name__)


async def _clear_running(owner: Any, profile: str, domain: str) -> None:
    """Fence admission, clear every running holder, and retain failures for relaunch."""
    async with owner._owner_lock:
        owner._pending_sites.add((profile, domain))
        records.started(profile, domain)
        runtimes = tuple(owner._sessions.values())
    confirmed = True
    holder_cleared = False
    release_fence = False
    try:
        pending = records.pending(profile).get(domain)
        if pending is None:
            release_fence = True
            return
        sites, generation = pending
        origins = tuple(records.origins(profile, domain))
        await asyncio.to_thread(_drop_kept, profile, domain)
        for runtime in runtimes:
            if await _profile(runtime) != profile:
                continue
            try:
                cleared = await _clear_runtime(runtime, sites, origins)
                holder_cleared |= cleared and runtime.record.profile == profile
            except Exception as exc:
                log.warning("site clear failed for %s (%s)", domain, type(exc).__name__)
                confirmed = False
                if not await _close_failed(owner, runtime):
                    return  # Keep the fence when the browser cannot be confirmed closed.
        if confirmed and holder_cleared:
            records.complete(profile, domain, generation)
        release_fence = True
    finally:
        if release_fence:
            async with owner._owner_lock:
                owner._pending_sites.discard((profile, domain))
