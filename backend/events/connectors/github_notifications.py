"""GitHub notifications poller: comments reach agents without a public webhook.

Backend loops read the person's notifications feed and, when connected, the
agents' account's feed: comments on agents' PRs notify their account. Each turns
new PR or issue comments into a webhook-shaped
payload, and hands it to ``admit_github_delivery_async``, the admission path
``POST /webhooks/github`` uses. Object-level dedupe in the delivery log means
an instance with both a webhook and this poller admits each comment once.
No model is called and agents never poll.

It is off under ``CORSIA_TEST_MODE=1`` — only an explicit
``CORSIA_GITHUB_NOTIFICATIONS_TEST_ENABLE=1`` opts an isolated, disposable
test instance back in, and the disable switch still wins over it — when
``CORSIA_GITHUB_NOTIFICATIONS_DISABLE=1``, when ``gh`` is missing, and when
``gh`` is not signed in (rechecked while off). Its state is logged on every
change and read by ``GET /api/system``.

Cursor: the first run records its start as the floor, so history is never
replayed. The cursor only moves past fully handled work: a comment is handled
once admitted or durably recorded as failed. A cycle that fully handles
everything stores GitHub's own clock as the next cursor plus the feed's
``Last-Modified``. A failed admission, a truncated read or a transient thread
error holds the cursor at or before the unhandled item, drops ``Last-Modified``
so the next read is unconditional, and reports the poller as degraded. A thread
GitHub permanently refuses (403, 404, or 410) is skipped,
logged and counted. A restarted instance resumes from the stored cursor. A short
lookback re-reads comments whose notification surfaced late; the delivery log's
object key keeps every re-read from admitting anything twice.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from connections.github_read_request import AUTH_PROBE, SIGNED_OUT
from events.connectors import github_notifications_feed as feed
from events.connectors.github_read_budget import budget, person_budget
import infra.webhook_delivery_log as webhook_delivery_log

log = logging.getLogger(__name__)

DISABLE_ENV = "CORSIA_GITHUB_NOTIFICATIONS_DISABLE"
#: Test-only opt-in: lets the poller run under ``CORSIA_TEST_MODE=1`` on an
#: isolated, disposable instance. Inert outside test mode; never beats the
#: disable switch.
TEST_ENABLE_ENV = "CORSIA_GITHUB_NOTIFICATIONS_TEST_ENABLE"
CURSOR_SETTING = "github_notifications_cursor"
AGENTS_CURSOR_SETTING = "github_notifications_agents_cursor"
#: GitHub's floor for this feed (``X-Poll-Interval``, verified 60 s).
MIN_INTERVAL_S = 60
JITTER_S = 10.0
MAX_BACKOFF_S = 900
#: While ``gh`` is missing or signed out, how often to look again.
RECHECK_S = 300
LOOKBACK = timedelta(minutes=5)
_STATIC_REASONS = frozenset({"test_mode", "config_off"})
#: Thread read failures that will not heal by retrying: skip the thread.
PERMANENT_THREAD_ERRORS = frozenset({"http_403", "http_404", "http_410"})

_status: dict[str, Any] = {
    "state": "starting", "reason": None, "last_success_at": None, "last_error": None,
    "skipped_threads": 0,
    "dropped_notifications": 0,
}

_agents_status: dict[str, Any] = dict(_status)


@dataclass(frozen=True, slots=True)
class Cursor:
    floor: datetime
    cursor: datetime
    last_modified: str | None


@dataclass(frozen=True, slots=True)
class CycleResult:
    """One poll: the interval GitHub asks for, and why the cycle was degraded."""

    interval: int
    degraded: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def poller_status(*, agents: bool = False) -> dict[str, Any]:
    """The selected feed's state, last success, error, counts and read budget."""
    status = _agents_status if agents else _status
    return {
        **status, "last_error": dict(status["last_error"] or {}) or None,
        "read_budget": (budget if agents else person_budget).status(),
    }


def _set_state(state: str, reason: str | None = None, *, agents: bool = False) -> None:
    status = _agents_status if agents else _status
    if (status["state"], status["reason"]) == (state, reason):
        return
    status["state"], status["reason"] = state, reason
    if state == "enabled":
        log.info("github-notifications: enabled")
    elif state == "disabled":
        log.info("github-notifications: disabled (%s)", reason)
    elif state == "degraded":
        log.warning("github-notifications: degraded (%s)", reason)
    else:
        log.warning("github-notifications: failing (%s)", reason)


def _is_signed_out(result: subprocess.CompletedProcess) -> bool:
    """Whether the sign-in probe explicitly reports no account on github.com."""
    return result.returncode != 0 and bool(SIGNED_OUT.search(f"{result.stdout}\n{result.stderr}"))


def disabled_reason(runner: feed.GhRunner) -> str | None:
    """Why the poller is off right now, or None when it should poll."""
    if os.environ.get("CORSIA_TEST_MODE") == "1" and os.environ.get(TEST_ENABLE_ENV) != "1":
        return "test_mode"
    if os.environ.get(DISABLE_ENV) == "1":
        return "config_off"
    try:
        result = runner(list(AUTH_PROBE), timeout=30.0)
    except FileNotFoundError:
        return "gh_missing"
    except Exception as exc:  # noqa: BLE001 — status failures must keep their own cause
        log.warning("github-notifications: gh auth status failed (%s)", type(exc).__name__)
        return "gh_error"
    if result.returncode == 0:
        return None
    if _is_signed_out(result):
        return "gh_signed_out"
    log.warning("github-notifications: gh auth status failed (exit %s)", result.returncode)
    return "gh_error"


def load_cursor(setting: str = CURSOR_SETTING) -> Cursor:
    """Read the durable cursor; the first run starts it, and its floor, at now."""
    from infra.settings import get_setting, set_setting

    stored = get_setting(setting) or {}
    floor, cursor = _parse_time(stored.get("floor")), _parse_time(stored.get("cursor"))
    if floor is not None and cursor is not None:
        return Cursor(floor, cursor, stored.get("last_modified"))
    now = _now()
    set_setting(setting, {"floor": feed.iso(now), "cursor": feed.iso(now)})
    return Cursor(now, now, None)


def _save_cursor(cursor: Cursor, setting: str = CURSOR_SETTING) -> None:
    from infra.settings import set_setting

    set_setting(setting, {
        "floor": feed.iso(cursor.floor),
        "cursor": feed.iso(cursor.cursor),
        "last_modified": cursor.last_modified,
    })


async def _admit(event_type: str, payload: dict[str, Any]) -> bool:
    """Admit one comment; return True once it is handled.

    Handled means admitted, or recorded in the delivery log (a failure after
    the row is written marks it failed there). A failure before the row exists
    leaves the comment unhandled, so the cursor holds and it is read again.
    """
    from events.connectors.github_admission import admit_github_delivery_async
    from infra.db_async import run_in_db

    comment_id = payload["comment"].get("id")
    try:
        await admit_github_delivery_async(
            event_type, payload,
            f"{webhook_delivery_log.NOTIFICATIONS_DELIVERY_PREFIX}{event_type}:{comment_id}",
            source="notifications",
        )
        return True
    except Exception:  # noqa: BLE001 — the durable log decides whether it is handled
        log.exception("github-notifications: admission failed for %s %s", event_type, comment_id)
    try:
        return await run_in_db(webhook_delivery_log.object_recorded, f"{event_type}:{comment_id}")
    except Exception:  # noqa: BLE001 — an unreadable log means not handled
        log.exception("github-notifications: delivery log unreadable for %s", comment_id)
        return False


async def _poll_thread(
    runner: feed.GhRunner, thread: dict[str, Any], since: datetime, cursor: datetime,
    *, agents: bool = False,
) -> tuple[datetime | None, str | None]:
    """Read and admit one thread; return (cursor hold, degraded reason)."""
    try:
        payloads = await asyncio.to_thread(
            feed.comment_payloads, runner, thread, since, webhook_delivery_log.object_recorded,
        )
    except feed.GhSignedOut:
        raise
    except feed.FeedError as exc:
        subject = (thread.get("subject") or {}).get("url")
        if exc.error_class in PERMANENT_THREAD_ERRORS:
            (_agents_status if agents else _status)["skipped_threads"] += 1
            log.warning("github-notifications: skipping %s (%s)", subject, exc.error_class)
            return None, None
        log.warning("github-notifications: holding for %s (%s)", subject, exc.error_class)
        return cursor, f"thread_{exc.error_class}"
    holds = [
        _parse_time(payload["comment"].get("created_at")) or cursor
        for event_type, payload in payloads
        if not await _admit(event_type, payload)
    ]
    return (min(holds), "admission_failed") if holds else (None, None)


async def poll_once(runner: feed.GhRunner, *, agents: bool = False) -> CycleResult:
    """Run one cycle; return GitHub's requested interval and any degradation."""
    from infra.db_async import run_in_db

    setting = AGENTS_CURSOR_SETTING if agents else CURSOR_SETTING
    status = _agents_status if agents else _status
    cursor = await run_in_db(load_cursor, setting)
    since = max(cursor.floor, cursor.cursor - LOOKBACK)
    page = await asyncio.to_thread(feed.list_notifications, runner, since, cursor.last_modified)
    interval = max(MIN_INTERVAL_S, page.poll_interval_s or MIN_INTERVAL_S)
    if page.not_modified:
        return CycleResult(interval)
    status["dropped_notifications"] += page.dropped_untracked
    holds: list[datetime] = [cursor.cursor] if page.truncated else []
    degraded = "truncated" if page.truncated else None
    for thread in page.threads:
        hold, reason = await _poll_thread(runner, thread, since, cursor.cursor, agents=agents)
        if hold is not None:
            holds.append(hold)
            degraded = degraded or reason
    next_cursor = min([page.server_time or _now(), *holds])
    last_modified = None if holds else page.last_modified
    await run_in_db(_save_cursor, Cursor(cursor.floor, next_cursor, last_modified), setting)
    return CycleResult(interval, degraded)


async def run_cycle(runner: feed.GhRunner, failures: int = 0, *, agents: bool = False) -> tuple[int | None, int]:
    """Check enablement and poll once; return (sleep seconds or None to stop, failures)."""
    status = _agents_status if agents else _status
    # Static switches take precedence without probing either account.
    reason: str | None
    if os.environ.get("CORSIA_TEST_MODE") == "1" and os.environ.get(TEST_ENABLE_ENV) != "1":
        reason = "test_mode"
    elif os.environ.get(DISABLE_ENV) == "1":
        reason = "config_off"
    elif agents:
        from connections import github_agent

        try:
            reason = "no_agents_account" if (await asyncio.to_thread(github_agent.view)).absent else None
        except Exception as exc:  # noqa: BLE001 — keep account lookup failures visible and retryable
            log.exception("github-notifications: agents' account lookup failed")
            return _record_failure(type(exc).__name__, failures, agents=True)
        if reason is None:
            reason = await asyncio.to_thread(disabled_reason, runner)
    else:
        reason = await asyncio.to_thread(disabled_reason, runner)
    if reason is not None:
        _set_state("disabled", reason, agents=agents)
        return (None if reason in _STATIC_REASONS else RECHECK_S), 0
    try:
        result = await poll_once(runner, agents=agents)
    except feed.GhSignedOut:
        _set_state("disabled", "gh_signed_out", agents=agents)
        return RECHECK_S, 0
    except feed.FeedError as exc:
        return _record_failure(exc.error_class, failures, agents=agents)
    except Exception as exc:  # noqa: BLE001 — a failing poller is visible, never silent
        log.exception("github-notifications: cycle failed")
        return _record_failure(type(exc).__name__, failures, agents=agents)
    if result.degraded is not None:
        status["last_error"] = {"class": result.degraded, "at": feed.iso(_now())}
        _set_state("degraded", result.degraded, agents=agents)
        return result.interval, 0
    status["last_success_at"] = feed.iso(_now())
    _set_state("enabled", agents=agents)
    return result.interval, 0


def _record_failure(error_class: str, failures: int, *, agents: bool = False) -> tuple[int, int]:
    status = _agents_status if agents else _status
    status["last_error"] = {"class": error_class, "at": feed.iso(_now())}
    _set_state("failing", error_class, agents=agents)
    failures += 1
    return min(MIN_INTERVAL_S * 2 ** min(failures, 4), MAX_BACKOFF_S), failures


async def notifications_poll_loop(runner: feed.GhRunner | None = None, *, agents: bool = False) -> None:
    """Poll until shut down, or until a static reason turns the poller off."""
    if runner is None:
        from connections.github_broker import default_broker, person_broker

        runner = default_broker() if agents else person_broker()
    failures = 0
    while True:
        delay, failures = await run_cycle(runner, failures, agents=agents)
        if delay is None:
            return
        await asyncio.sleep(delay + random.uniform(0, JITTER_S))
