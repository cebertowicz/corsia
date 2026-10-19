"""Per-Microsoft-mailbox inbox poller. Its cursor belongs to the connected-account row."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

log = logging.getLogger(__name__)

from connections.microsoft import mail
from infra.database import write_transaction
from infra.db_async import run_in_db
from infra.time import utc_now_iso

POLL_INTERVAL_S = 120
_inflight: dict[str, asyncio.Task[None]] = {}


def cancel_account(account_id: str) -> None:
    task = _inflight.pop(account_id, None)
    if task is not None and not task.done() and task is not asyncio.current_task():
        task.cancel()


def _cursor(account_id: str) -> dict[str, object] | None:
    from connections.connected_accounts import get_account
    from infra.database import get_db
    db = get_db()
    try:
        row = get_account(account_id, db=db)
        value = row.get("cursor")
        return value if isinstance(value, dict) else None
    except Exception as exc:
        log.warning("microsoft poller could not read its account cursor: %s", type(exc).__name__)
        return None
    finally:
        db.close()


def _save_cursor(account_id: str, cursor: dict[str, object]) -> bool:
    with write_transaction(None, scope="microsoft_poller_cursor") as db:
        result = db.execute("UPDATE connected_accounts SET cursor_json=?, updated_at=? WHERE id=? AND provider='microsoft' AND state='connected'", (json.dumps(cursor, separators=(",", ":")), utc_now_iso(), account_id))
        return result.rowcount == 1


async def poll_once(account_id: str) -> None:
    """Screen new inbox headers; first poll establishes a floor and never replays mail."""
    messages = await mail.search(account_id, "folder:inbox", 25)
    cursor = await run_in_db(_cursor, account_id)
    newest = messages[0].message_id if messages else None
    if cursor is None:
        await run_in_db(_save_cursor, account_id, {"kind": "microsoft", "last_message_id": newest, "last_success_at": utc_now_iso()})
        return
    seen = cursor.get("last_message_id")
    for message in messages:
        if message.message_id == seen:
            break
        # Never deliver provider text without the shared local screen. The screen record
        # is also the durable input for a later explicit microsoft_mail_read call.
        from connections.google import email_screen
        await email_screen.screen_message(account_id, message.message_id, message.headers, message.body)
    await run_in_db(_save_cursor, account_id, {"kind": "microsoft", "last_message_id": newest, "last_success_at": utc_now_iso()})


async def _run(account_id: str) -> None:
    try:
        await poll_once(account_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("microsoft poller cycle failed: %s", type(exc).__name__)
    finally:
        _inflight.pop(account_id, None)


def start_account(account_id: str) -> None:
    """Schedule an immediate one-account poll; active accounts are never duplicated."""
    task = _inflight.get(account_id)
    if task is None or task.done():
        _inflight[account_id] = asyncio.create_task(_run(account_id))
