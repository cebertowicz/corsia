"""The mail-host half of the shared inbox poller.

The durable cursor is ``UIDVALIDITY:last UID``.  Initializing it or observing a
new UIDVALIDITY establishes a floor; neither path emits historical mail.
"""

from __future__ import annotations

import asyncio

from connections.mailhost import imap, read
from infra.db_async import run_in_db

MAX_POLL_BYTES = 500_000


def _parse_cursor(value: str) -> tuple[int, int]:
    parts = value.split(":", 1)
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise ValueError("invalid mail-host inbox cursor")
    return int(parts[0]), int(parts[1])


def _cursor(validity: int, uid: int) -> str:
    return f"{validity}:{uid}"


async def floor(account_id: str) -> str:
    """Record the current mailbox position without delivering existing mail."""
    endpoints, username, password = await run_in_db(read.credentials, account_id)

    def call() -> tuple[int, int]:
        with imap.account_lock(account_id):
            return imap.inbox_cursor(endpoints, username, password)

    validity, uid = await read._guarded(account_id, endpoints, call)
    return _cursor(validity, uid)


async def added(account_id: str, position: str) -> tuple[str, list[read.Message]]:
    """Return a new cursor and messages newer than ``position``; never replay a rebuilt inbox."""
    validity, last_uid = _parse_cursor(position)
    endpoints, username, password = await run_in_db(read.credentials, account_id)

    def call() -> tuple[int, int, list[tuple[int, bytes]]]:
        with imap.account_lock(account_id):
            return imap.added_after(endpoints, username, password, validity, last_uid, MAX_POLL_BYTES)

    current_validity, current_last, found = await read._guarded(account_id, endpoints, call)
    return (
        _cursor(current_validity, current_last),
        [read.Message(f"{current_validity}-{uid}", "", *read.parse(raw, with_body=True)) for uid, raw in found],
    )
