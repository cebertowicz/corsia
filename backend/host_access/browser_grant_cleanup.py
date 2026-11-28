"""Clear a granted site from every browser holding it, and fail closed when that can't be confirmed."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from host_access import browser_grant_copy, browser_grant_records as records, browser_site_clear
from host_access.browser_session_state import COMMAND_TIMEOUT, BrowserSessionError
from infra.product_events import emit_product_event

log = logging.getLogger(__name__)
LAUNCH = "launch"  # the end cause of a clear owed at launch: a restart, or an earlier clear that did not finish


class PendingClearError(RuntimeError):
    """A pending site's launch-time clear did not complete."""


class _Superseded(RuntimeError):
    """A newer end or origin arrived while the site was cleared."""


def require_unfenced(runtime: Any, *, pane: bool = False) -> None:
    """Refuse an agent command on a browser holding a profile whose site is being cleared.

    The person's own pane commands are never fenced.
    """
    if not pane and records.fenced(runtime.record.site_profile):
        raise BrowserSessionError("browser session unavailable")


async def _emit(outcome: str, cause: str) -> None:
    await asyncio.to_thread(emit_product_event, "browser_signout", "site_clear",
                            properties={"outcome": outcome, "cause": cause})


async def _clear_site(endpoint: str, site: str, origins: tuple[str, ...]) -> None:
    if origins:
        await browser_site_clear.clear(endpoint, site, recorded_origins=origins)
    else:
        await browser_site_clear.clear(endpoint, site)


async def recover_launch(runtime: Any) -> None:
    """Clear PENDING sites before a saved-profile launch serves any command."""
    profile = runtime.record.profile
    if not profile:
        return
    try:
        for domain, (sites, generation) in (await asyncio.to_thread(records.pending, profile)).items():
            await asyncio.to_thread(browser_grant_copy.drop, profile, domain)
            known = tuple(await asyncio.to_thread(records.origins, profile, domain))
            for site in sites:
                await _clear_site(runtime.chrome.endpoint, site, known)
            if not await asyncio.to_thread(records.complete, profile, domain, generation):
                raise _Superseded("site clear superseded")
            await _emit("cleared", LAUNCH)
    except Exception as exc:
        log.warning("site clear at launch failed (%s)", type(exc).__name__)
        await _emit("superseded" if isinstance(exc, _Superseded) else "clear_failed", LAUNCH)
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


async def _close_failed(owner: Any, runtime: Any, cause: str) -> bool:
    """Close a browser whose clear failed, terminating Chrome if the close is not confirmed."""
    try:
        result = await asyncio.wait_for(owner._close_one(runtime), COMMAND_TIMEOUT)
        if result is None:
            await _emit("browser_closed", cause)
            return True
        log.warning("browser close after a failed site clear was not confirmed (%s)", type(result).__name__)
    except Exception as exc:
        log.warning("browser close after a failed site clear was not confirmed (%s)", type(exc).__name__)
    # Cancellation propagates: the caller then keeps the record and fence, so commands stay refused.
    try:
        if runtime.chrome is not None:
            await asyncio.wait_for(runtime.chrome.close(), COMMAND_TIMEOUT)
            if await asyncio.wait_for(owner._close_one(runtime), COMMAND_TIMEOUT) is None:
                await _emit("browser_closed", cause)
                return True
    except Exception as exc:
        log.warning("browser termination after a failed site clear failed (%s)", type(exc).__name__)
    return False


async def _holders(owner: Any, profile: str) -> tuple[Any, ...]:
    async with owner._owner_lock:
        return tuple(runtime for runtime in owner._sessions.values() if runtime.record.site_profile == profile)


async def clear_running(owner: Any, profile: str, domain: str, cause: str) -> None:
    """Clear one ended grant's site; release its fence unless a browser can't be confirmed closed."""
    async with owner._grant_cleanup_lock:
        release = False
        try:
            release = await _clear_running(owner, profile, domain, cause)
        except Exception as exc:
            log.warning("site clear failed for %s (%s)", domain, type(exc).__name__)
            await _emit("clear_failed", cause)
            release = True
            for runtime in await _holders(owner, profile):
                release = await _close_failed(owner, runtime, cause) and release
        finally:
            if release:
                records.release(profile, domain)


async def _clear_running(owner: Any, profile: str, domain: str, cause: str) -> bool:
    """Clear every running holder; keep the record unless all were cleared. False keeps the fence."""
    entry = (await asyncio.to_thread(records.pending, profile)).get(domain)
    if entry is None:
        return True  # A newer clear or a launch already cleared it.
    sites, generation = entry
    origins = tuple(await asyncio.to_thread(records.origins, profile, domain))
    await asyncio.to_thread(browser_grant_copy.drop, profile, domain)
    confirmed = True
    holder_cleared = False
    for runtime in await _holders(owner, profile):
        try:
            cleared = await _clear_runtime(runtime, sites, origins)
            holder_cleared |= cleared and runtime.record.profile == profile
        except Exception as exc:
            log.warning("site clear failed for %s (%s)", domain, type(exc).__name__)
            await _emit("clear_failed", cause)
            confirmed = False
            if not await _close_failed(owner, runtime, cause):
                return False
    if not confirmed:
        return True  # The record stays for the next launch; every failed browser was closed.
    if not holder_cleared:
        await _emit("deferred", cause)  # No saved-profile browser is running; its next launch clears.
        return True
    completed = await asyncio.to_thread(records.complete, profile, domain, generation)
    await _emit("cleared" if completed else "superseded", cause)
    return True
