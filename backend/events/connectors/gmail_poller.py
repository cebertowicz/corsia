"""Platform Gmail poller: screen new Inbox mail and deliver generic events.

The Google client owns credentials, the screen owns its stored result, and
``events.platform`` owns idempotent routing. This loop owns only the durable
Gmail history cursor and its observable state. A first run records Gmail's
current history id as a floor, so it never replays old mail.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

from connections.google import client as google_client
from connections.google import email_screen, oauth
from events.connectors import gmail_feed as feed
from events.platform import deliver_platform_event
from infra.db_async import run_in_db
from infra.time import utc_now_iso

log = logging.getLogger(__name__)

CURSOR_SETTING = "gmail_poll_cursor"
DISABLE_ENV = "CORSIA_GMAIL_POLLER_DISABLE"
TEST_ENABLE_ENV = "CORSIA_GMAIL_POLLER_TEST_ENABLE"
POLL_INTERVAL_S = 120
JITTER_S = 10.0
RECHECK_S = 300
BACKOFF_S = 900
_STATIC_REASONS = frozenset({"test_mode", "config_off"})

_status: dict[str, Any] = {
    "state": "starting",
    "reason": None,
    "last_success_at": None,
    "last_error": None,
}


@dataclass(frozen=True)
class Cursor:
    account: str
    floor_at: str
    history_id: str
    last_success_at: str | None = None


def poller_status() -> dict[str, Any]:
    """A copy of live poller state for Google and system status surfaces."""
    return {**_status, "last_error": dict(_status["last_error"] or {}) or None}


def _set_state(state: str, reason: str | None = None) -> None:
    if (_status["state"], _status["reason"]) == (state, reason):
        return
    _status["state"], _status["reason"] = state, reason
    if state == "enabled":
        log.info("gmail-poller: enabled")
    elif state == "disabled":
        log.info("gmail-poller: disabled (%s)", reason)
    else:
        log.warning("gmail-poller: %s (%s)", state, reason)


def _load_stored_cursor(account: str) -> Cursor | None:
    """Load the single compatibility account's cursor from its account row."""
    from connections.connected_accounts import google_account
    from infra.database import get_db

    db = get_db()
    try:
        connected = google_account(db=db)
        stored = connected["cursor"] if connected and connected["address"] == account else None
    finally:
        db.close()
    if not isinstance(stored, dict):
        return None
    floor_at, history_id = stored.get("floor_at"), stored.get("history_id")
    if not isinstance(floor_at, str) or not isinstance(history_id, str) or not history_id:
        return None
    last_success_at = stored.get("last_success_at")
    return Cursor(account, floor_at, history_id, last_success_at if isinstance(last_success_at, str) else None)


def _save_cursor(cursor: Cursor) -> None:
    from connections.connected_accounts import google_account, set_cursor
    from infra.database import get_db

    db = get_db()
    try:
        connected = google_account(db=db)
        if connected is None or connected["address"] != cursor.account:
            return
        set_cursor(str(connected["id"]), {
            "account": cursor.account,
            "floor_at": cursor.floor_at,
            "history_id": cursor.history_id,
            "last_success_at": cursor.last_success_at,
        }, db=db)
    finally:
        db.close()


async def _load_cursor(account: str) -> tuple[Cursor, bool]:
    """Return ``(cursor, initialized)``; a new floor deliberately skips this cycle."""
    stored = await run_in_db(_load_stored_cursor, account)
    if stored is not None:
        return stored, False
    now = utc_now_iso()
    cursor = Cursor(account, now, await feed.profile_history_id())
    await run_in_db(_save_cursor, cursor)
    return cursor, True


def _date(headers: dict[str, str]) -> str | None:
    raw = headers.get("date")
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
        return parsed.isoformat() if parsed is not None else None
    except (TypeError, ValueError):
        return None


def event_payload(
    message: Any, screen: email_screen.Screen, history_id: str,
) -> dict[str, Any]:
    """Flat Gmail facts for the declared manifest; bodies never enter this payload."""
    headers = message.headers
    payload: dict[str, Any] = {
        "message_id": message.message_id,
        "thread_id": message.thread_id,
        "history_id": history_id,
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "label": "INBOX",
        "screen_status": screen.status,
        "needs_person": screen.needs_person,
        "injection": bool(screen.injection),
        "injection_rule": screen.rule,
        "kind": screen.kind,
        "screen_model": screen.model_id,
    }
    if date := _date(headers):
        payload["date"] = date
    if screen.clean and (subject := headers.get("subject")):
        payload["subject"] = subject
    return payload


async def disabled_reason() -> str | None:
    """Why polling is off; credential state is owned by the existing Google sign-in."""
    if os.environ.get("CORSIA_TEST_MODE") == "1" and os.environ.get(TEST_ENABLE_ENV) != "1":
        return "test_mode"
    if os.environ.get(DISABLE_ENV) == "1":
        return "config_off"
    account = await run_in_db(oauth.account_status)
    status = account.get("status")
    if status == google_client.STATUS_NEEDS_SIGN_IN:
        return google_client.STATUS_NEEDS_SIGN_IN
    if status != google_client.STATUS_CONNECTED:
        return "not_connected"
    return None


async def poll_once(account: str) -> None:
    """Deliver one complete Gmail history page, holding the cursor on any failure."""
    cursor, initialized = await _load_cursor(account)
    if initialized:
        now = utc_now_iso()
        await run_in_db(_save_cursor, Cursor(account, cursor.floor_at, cursor.history_id, now))
        await run_in_db(email_screen.sweep_expired)
        return
    page = await feed.added_messages(cursor.history_id)
    for message_id in page.message_ids:
        try:
            metadata = await feed.metadata(message_id)
            full = await feed.full_message(message_id)
        except feed.GmailMessageGone:
            continue
        screen = await email_screen.screen_message(account, message_id, full.headers, full.body)
        await deliver_platform_event("gmail", event_payload(metadata, screen, page.history_id))
    now = utc_now_iso()
    await run_in_db(_save_cursor, Cursor(account, cursor.floor_at, page.history_id, now))
    await run_in_db(email_screen.sweep_expired)


async def _reset_expired_cursor(account: str) -> None:
    """Skip expired Gmail history without replaying mail or emitting a gap digest."""
    cursor = Cursor(account, utc_now_iso(), await feed.profile_history_id(), utc_now_iso())
    await run_in_db(_save_cursor, cursor)
    _status["last_success_at"] = cursor.last_success_at


async def run_cycle() -> int | None:
    """Run once; return next delay or ``None`` when a static configuration turns it off."""
    reason = await disabled_reason()
    if reason is not None:
        _set_state("disabled", reason)
        return None if reason in _STATIC_REASONS else RECHECK_S
    account = await run_in_db(oauth.account_status)
    email = account.get("email")
    if not isinstance(email, str) or not email:
        _set_state("disabled", "not_connected")
        return RECHECK_S
    try:
        await poll_once(email)
    except feed.GmailHistoryExpired:
        try:
            await _reset_expired_cursor(email)
        except google_client.GoogleSignInRejected:
            _set_state("disabled", google_client.STATUS_NEEDS_SIGN_IN)
            return RECHECK_S
        except Exception as exc:  # noqa: BLE001 -- reset failures must remain visible
            _status["last_error"] = {"class": type(exc).__name__, "at": utc_now_iso()}
            _set_state("failing", type(exc).__name__)
            return BACKOFF_S
    except google_client.GoogleSignInRejected:
        _set_state("disabled", google_client.STATUS_NEEDS_SIGN_IN)
        return RECHECK_S
    except Exception as exc:  # noqa: BLE001 -- the cursor intentionally remains unchanged
        error_class = getattr(exc, "error_class", type(exc).__name__)
        _status["last_error"] = {"class": error_class, "at": utc_now_iso()}
        _set_state("failing", error_class)
        return BACKOFF_S
    _status["last_success_at"] = utc_now_iso()
    _status["last_error"] = None
    _set_state("enabled")
    return POLL_INTERVAL_S


async def gmail_poll_loop() -> None:
    """Run until a static off-state; transient failures retry at the fixed 15-minute backoff."""
    while True:
        delay = await run_cycle()
        if delay is None:
            return
        await asyncio.sleep(delay + random.uniform(0, JITTER_S))
