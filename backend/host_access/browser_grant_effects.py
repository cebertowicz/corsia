"""Bridge synchronous fill-grant transitions to browser cleanup."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from host_access import browser_grant_records as records, browser_launch
from host_access.browser_domain import url_domain
from infra import app_loop
from infra.product_events import emit_product_event

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
        end(item, lambda: None, "not_granted")
        raise


def start_timer(item: Any, expire: Callable[[str], None]) -> None:
    delay = max(0.0, item.deadline - time.monotonic())
    timer = threading.Timer(delay, expire, args=(item.id,))
    timer.daemon = True
    with _timer_lock:
        _timers[item.id] = timer
    timer.start()


def end(item: Any, commit: Callable[[], Any], cause: str) -> None:
    """Make the site PENDING, commit the grant end, then schedule its clear; ``cause`` names why it ended."""
    target = profile(item)
    domain = url_domain(item.site)
    records.end(target, item.site)
    try:
        commit()
    except Exception as exc:
        log.warning("saved-login grant end failed for %s (%s)", domain, type(exc).__name__)
        raise
    with _timer_lock:
        timer = _timers.pop(item.id, None)
    if timer is not None:
        timer.cancel()
    assert domain is not None
    try:
        app_loop.submit(_clear(target, domain, cause))
    except app_loop.AppLoopUnavailable:
        # No session can be served without the app loop; the record stays PENDING for launch.
        records.release(target, domain)
        log.warning("site clear awaits browser startup for %s", domain)
        emit_product_event("browser_signout", "site_clear", properties={"outcome": "deferred", "cause": cause})


async def _clear(target: str, domain: str, cause: str) -> None:
    from host_access.browser_grant_cleanup import clear_running
    from host_access.processes import PROCESS_OWNER
    await clear_running(PROCESS_OWNER.browser_sessions(), target, domain, cause)
