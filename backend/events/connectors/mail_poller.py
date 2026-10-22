"""Shared Gmail/Microsoft mail poller.

Provider clients own credentials, the screen owns its stored result, and
``events.platform`` owns idempotent routing. This loop owns each account's
durable provider cursor and observable state. A first run
records the mailbox's current position as a floor, so it never replays old
mail. Each cycle snapshots the connected accounts and polls each one in its
own task. Remove cancels that task; otherwise the writes own the guard: the
screen record, the event and the cursor each re-check the account inside
their own transaction, and a fetch for a signed-out or removed account gets
no credential.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from connections.google import client as google_client
from connections.mail import email_screen
from connections.microsoft import client as microsoft_client
from connections.microsoft.errors import MicrosoftError, REFRESH_TOKEN_INVALID
from events.connectors.mail_poll_read import ADAPTERS, event_payload
from events.connectors import gmail_feed as feed
from events.platform import DIGESTED, FILTERED, WOKE, PlatformDelivery, deliver_platform_event_sync
from events.resolve import Target
from infra.database import get_db, write_transaction
from infra.db_async import run_in_db
from infra.time import utc_now_iso

log = logging.getLogger(__name__)

DISABLE_ENV = "CORSIA_MAIL_POLLER_DISABLE"
TEST_ENABLE_ENV = "CORSIA_MAIL_POLLER_TEST_ENABLE"
POLL_INTERVAL_S = 120
JITTER_S = 10.0
RECHECK_S = 300
BACKOFF_S = 900
_STATIC_REASONS = frozenset({"test_mode", "config_off"})


def _fresh_status() -> dict[str, Any]:
    return {
        "state": "starting", "reason": None, "last_success_at": None, "last_error": None,
        "delivery": {"delivered": 0, "excluded": 0, "outcomes": {}},
    }


#: The loop's own state: why it is off, if it is.
_status: dict[str, Any] = _fresh_status()
#: Per-account state, in-flight task and next due time; a removed account leaves all three.
_accounts: dict[str, dict[str, Any]] = {}
_inflight: dict[str, asyncio.Task] = {}
_due: dict[str, float] = {}


@dataclass(frozen=True)
class Cursor:
    account_id: str
    floor_at: str
    history_id: str
    last_success_at: str | None = None
    provider: str = "google"


def poller_status(account_id: str | None = None) -> dict[str, Any]:
    """A copy of live poller state: the loop's, or one account's (``starting`` until it is polled)."""
    source = _status if account_id is None else _accounts.get(account_id, _fresh_status())
    delivery = source.get("delivery", {})
    return {
        **source, "last_error": dict(source["last_error"] or {}) or None,
        "delivery": {**delivery, "outcomes": dict(delivery.get("outcomes", {}))},
    }


def _set_state(state: str, reason: str | None = None, account_id: str | None = None) -> None:
    target = _status if account_id is None else _accounts.setdefault(account_id, _fresh_status())
    if (target["state"], target["reason"]) == (state, reason):
        return
    target["state"], target["reason"] = state, reason
    label = "mail-poller" if account_id is None else f"mail-poller[{account_id}]"
    if state == "enabled":
        log.info("%s: enabled", label)
    elif state == "disabled":
        log.info("%s: disabled (%s)", label, reason)
    else:
        log.warning("%s: %s (%s)", label, state, reason)


def cancel_account(account_id: str) -> None:
    """Stop one account's in-flight poll and forget its state; other accounts keep polling."""
    task = _inflight.pop(account_id, None)
    if task is not None and not task.done() and task is not asyncio.current_task():
        task.cancel()
    _accounts.pop(account_id, None)
    _due.pop(account_id, None)


def schedule_account(account_id: str) -> None:
    """Mark a newly signed-in account due; the startup-owned loop does the work."""
    _due.pop(account_id, None)


def _account_states() -> dict[str, str]:
    from connections.connected_accounts import list_accounts

    db = get_db()
    try:
        return {
            str(account["id"]): str(account["state"])
            for provider in ADAPTERS
            for account in list_accounts(provider, db=db)
        }
    finally:
        db.close()


def _load_stored_cursor(account_id: str) -> Cursor | None:
    """Load one connected account's own cursor from its row."""
    from connections.connected_accounts import ConnectedAccountError, get_account

    db = get_db()
    try:
        account = get_account(account_id, db=db)
        stored = account["cursor"]
    except ConnectedAccountError:
        return None
    finally:
        db.close()
    if not isinstance(stored, dict):
        return None
    provider = str(account["provider"])
    position_key = ADAPTERS[provider].cursor_key
    floor_at, history_id = stored.get("floor_at"), stored.get(position_key)
    if not isinstance(floor_at, str) or not isinstance(history_id, str) or not history_id:
        return None
    last_success_at = stored.get("last_success_at")
    return Cursor(account_id, floor_at, history_id, last_success_at if isinstance(last_success_at, str) else None, provider)


def _save_cursor(cursor: Cursor, db: sqlite3.Connection | None = None) -> bool:
    """Write the cursor on its account's row only while that account is connected."""
    from connections.connected_accounts import is_connected
    from connections.account_state import set_cursor

    with write_transaction(db, scope="mail_poll_cursor") as conn:
        if not is_connected(cursor.account_id, db=conn):
            return False
        set_cursor(cursor.account_id, {
            "floor_at": cursor.floor_at,
            ADAPTERS[cursor.provider].cursor_key: cursor.history_id,
            "last_success_at": cursor.last_success_at,
        }, db=conn)
        return True


def _deliver(account_id: str, payload: dict[str, Any], db: sqlite3.Connection | None = None) -> bool:
    """Deliver one event only to resolved owners inside its account reach."""
    from connections.connected_accounts import account_reaches_thread, get_account, is_connected

    def inside_account_reach(target: Target, connection: sqlite3.Connection) -> str | None:
        if target.thread is None or account_reaches_thread(account_id, target.thread, db=connection):
            return None
        return "account_out_of_reach"

    with write_transaction(db, scope="mail_poll_deliver") as conn:
        if not is_connected(account_id, db=conn):
            return False
        provider = get_account(account_id, db=conn)["provider"]
        deliveries = deliver_platform_event_sync(
            ADAPTERS[str(provider)].connector, payload, target_filter=inside_account_reach, db=conn,
        )
        _record_delivery(account_id, deliveries)
        return True


def _record_delivery(account_id: str, deliveries: list[PlatformDelivery]) -> None:
    """Expose mail routing outcomes without treating an intentional scope drop as a poll failure."""
    status = _accounts.setdefault(account_id, _fresh_status())
    delivery = status["delivery"]
    outcomes = delivery["outcomes"]
    for result in deliveries:
        outcomes[result.outcome] = outcomes.get(result.outcome, 0) + 1
        if result.outcome == FILTERED:
            delivery["excluded"] += 1
        elif result.outcome in {WOKE, DIGESTED}:
            delivery["delivered"] += 1


def _provider(account_id: str) -> str:
    from connections.connected_accounts import get_account

    db = get_db()
    try:
        return str(get_account(account_id, db=db)["provider"])
    finally:
        db.close()


async def _new_cursor(account_id: str) -> Cursor:
    provider = await run_in_db(_provider, account_id)
    position = await ADAPTERS[provider].floor(account_id)
    now = utc_now_iso()
    return Cursor(account_id, now, position, now, provider)


async def _load_cursor(account_id: str) -> tuple[Cursor | None, bool]:
    """Return ``(cursor, initialized)``; a new floor deliberately skips this cycle."""
    stored = await run_in_db(_load_stored_cursor, account_id)
    if stored is not None:
        return stored, False
    cursor = await _new_cursor(account_id)
    return (cursor, True) if await run_in_db(_save_cursor, cursor) else (None, True)



def disabled_reason() -> str | None:
    """Why the whole loop is off by configuration; each account's own state is separate."""
    if os.environ.get("CORSIA_TEST_MODE") == "1" and os.environ.get(TEST_ENABLE_ENV) != "1":
        return "test_mode"
    if os.environ.get(DISABLE_ENV) == "1":
        return "config_off"
    return None



async def poll_once(account_id: str) -> None:
    """Screen and deliver one provider page, holding the cursor on any failure.

    Returns early, writing nothing more, once a write finds the account no longer connected.
    """
    cursor, initialized = await _load_cursor(account_id)
    if initialized or cursor is None:
        await run_in_db(email_screen.sweep_expired)
        return
    adapter = ADAPTERS[cursor.provider]
    position, page = await adapter.read(account_id, cursor.history_id)
    async for metadata, full in adapter.messages(account_id, page):
        screen = await email_screen.screen_message(account_id, full.message_id, full.headers, full.body)
        payload = event_payload(account_id, metadata, screen, position, cursor.provider)
        if not await run_in_db(_deliver, account_id, payload):
            return
    updated = Cursor(account_id, cursor.floor_at, position, utc_now_iso(), cursor.provider)
    if await run_in_db(_save_cursor, updated):
        await run_in_db(email_screen.sweep_expired)


async def _reset_expired_cursor(account_id: str) -> None:
    """Reset an expired provider cursor without replaying mail or emitting a gap digest."""
    cursor = await _new_cursor(account_id)
    if await run_in_db(_save_cursor, cursor):
        _accounts.setdefault(account_id, _fresh_status())["last_success_at"] = cursor.last_success_at


async def _run_account(account_id: str) -> int:
    """Poll one account once; return its next delay. Its failures never touch another account."""
    status = _accounts.setdefault(account_id, _fresh_status())
    try:
        try:
            await poll_once(account_id)
        except (feed.GmailHistoryExpired, microsoft_client.DeltaExpired):
            await _reset_expired_cursor(account_id)
    except MicrosoftError as exc:
        if exc.cause == REFRESH_TOKEN_INVALID:
            _set_state("disabled", (await run_in_db(_account_states)).get(account_id), account_id)
            return RECHECK_S
        status["last_error"] = {"class": exc.cause, "at": utc_now_iso()}
        _set_state("failing", exc.cause, account_id)
        return BACKOFF_S
    except google_client.GoogleSignInRejected:
        _set_state("disabled", (await run_in_db(_account_states)).get(account_id), account_id)
        return RECHECK_S
    except google_client.GoogleNotConnected:
        return RECHECK_S  # Signed out or removed mid-poll; the next snapshot settles it.
    except Exception as exc:  # noqa: BLE001 -- the cursor intentionally remains unchanged
        error_class = getattr(exc, "error_class", type(exc).__name__)
        status["last_error"] = {"class": error_class, "at": utc_now_iso()}
        _set_state("failing", error_class, account_id)
        return BACKOFF_S
    status["last_success_at"] = utc_now_iso()
    status["last_error"] = None
    _set_state("enabled", None, account_id)
    return POLL_INTERVAL_S


async def run_cycle() -> int | None:
    """Poll every due connected account; return the next delay, or ``None`` when configuration turns it off."""
    reason = disabled_reason()
    if reason is not None:
        _set_state("disabled", reason)
        return None if reason in _STATIC_REASONS else RECHECK_S
    states = await run_in_db(_account_states)
    connected = [account_id for account_id, state in states.items() if state == "connected"]
    for known in set(_accounts) | set(_due):
        if known not in states:
            cancel_account(known)
        elif known not in connected:
            cancel_account(known)
    if not connected:
        _set_state("disabled", "not_connected")
        return RECHECK_S
    _set_state("enabled")
    now = time.monotonic()
    due = [account_id for account_id in connected if _due.get(account_id, 0.0) <= now]
    tasks = {account_id: asyncio.create_task(_run_account(account_id)) for account_id in due}
    _inflight.update(tasks)
    for account_id, task in tasks.items():
        try:
            delay = await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if not task.cancelled() or (current is not None and current.cancelling()):
                raise
            continue  # Removed mid-poll: cancel_account already forgot it.
        finally:
            if _inflight.get(account_id) is task:
                _inflight.pop(account_id, None)
        _due[account_id] = time.monotonic() + delay
    upcoming = [_due[account_id] - time.monotonic() for account_id in connected if account_id in _due]
    return max(1, int(min(upcoming))) if upcoming else POLL_INTERVAL_S


async def mail_poll_loop() -> None:
    """Run until a static off-state; each account retries at its own fixed backoff."""
    while True:
        delay = await run_cycle()
        if delay is None:
            return
        await asyncio.sleep(delay + random.uniform(0, JITTER_S))
