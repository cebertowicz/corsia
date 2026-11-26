"""Bridge synchronous fill-grant transitions to browser cleanup."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from host_access import browser_grant_records as records, browser_launch
from host_access.browser_domain import url_domain
from infra import app_loop

log = logging.getLogger(__name__)
_timers: dict[str, threading.Timer] = {}
_timer_lock = threading.Lock()


def profile(item: Any) -> str:
    """Derive the same profile as the thread-owner resolver, without persisting identity."""
    if not isinstance(item.user_id, str) or not browser_launch._OWNER_KEY.fullmatch(item.user_id):
        raise ValueError("browser owner unavailable")
    return str(browser_launch.profile_root() / item.user_id)


def arm(item: Any) -> None:
    records.arm(profile(item), item.site)


def arm_and_schedule(item: Any, expire: Callable[[str], None]) -> None:
    arm(item)
    if item.standing:
        return
    try:
        start_timer(item, expire)
    except BaseException:
        end(item, lambda: None)
        raise


def start_timer(item: Any, expire: Callable[[str], None]) -> None:
    delay = max(0.0, item.deadline - time.monotonic())
    timer = threading.Timer(delay, expire, args=(item.id,))
    timer.daemon = True
    with _timer_lock:
        _timers[item.id] = timer
    timer.start()


def end(item: Any, commit: Callable[[], Any]) -> None:
    """Journal, commit the grant end, then schedule browser cleanup."""
    target = profile(item)
    records.end(target, item.site)
    try:
        commit()
    except Exception as exc:
        log.warning("saved-login grant end failed for %s (%s)", item.site, type(exc).__name__)
        raise
    with _timer_lock:
        timer = _timers.pop(item.id, None)
    if timer is not None:
        timer.cancel()
    domain = url_domain(item.site)
    assert domain is not None
    try:
        app_loop.submit(_clear(target, domain))
    except app_loop.AppLoopUnavailable:
        # No session can be served without the app loop; the journal remains for launch.
        records.started(target, domain)
        log.warning("site clear awaits browser startup for %s", domain)


async def _clear(target: str, domain: str) -> None:
    from host_access.browser_grant_cleanup import clear_running
    from host_access.processes import PROCESS_OWNER
    await clear_running(PROCESS_OWNER.browser_sessions(), target, domain)
