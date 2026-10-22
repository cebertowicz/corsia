"""The check that runs when the person sets or replaces a mail-host app password.

It starts only from the Keys write path (``api.keys``) and the row's own Try again and Edit
server (``api.mail_host_accounts``), all behind the person's session and password; no
agent-callable route reaches it (H7, H17). IMAP login first (a refused password
stops there, so one wrong password is one login attempt at the host), then SMTP, then CalDAV
discovery. A calendar the host doesn't offer leaves the row mail only. Every failure becomes
one named status; the host's own text never reaches the row, a log or an agent (H15).
"""

from __future__ import annotations

import asyncio
import logging

from connections.account_state import emit_lifecycle_event
from connections.connected_accounts import resolve_credentials
from connections.mailhost import caldav, imap, smtp, store
from connections.mailhost.endpoints import Endpoints
from connections.mailhost.errors import IMAP_OFF, PASSWORD_REFUSED, MailHostError
from connections.mailhost.provider import PASSWORD
from infra.db_async import run_in_db

log = logging.getLogger(__name__)

_STATUS_OF_CAUSE = {PASSWORD_REFUSED: store.REFUSED, IMAP_OFF: store.IMAP_OFF}
_pending: set[asyncio.Task[str]] = set()


def _mail_login(account_id: str, endpoints: Endpoints, password: str) -> Endpoints:
    """IMAP then SMTP login in this worker thread; the endpoints with the username the host accepted."""
    with imap.account_lock(account_id):
        username = imap.check_login(endpoints, password)
    smtp.check_login(endpoints, password)
    return endpoints.with_found(imap_username=username)


async def _calendars(endpoints: Endpoints, password: str) -> Endpoints:
    """The endpoints with the calendars discovery found; unchanged and calendar-less when there are none."""
    try:
        username, urls = await caldav.discover(endpoints, password)
    except caldav.CalendarUnavailable:
        return endpoints.with_found(calendars=(), caldav_username=None)
    return endpoints.with_found(calendars=urls, caldav_username=username)


async def _outcome(account_id: str, endpoints: Endpoints, password: str) -> tuple[Endpoints, str, str, tuple[str, ...]]:
    """``(endpoints, status, failing host, capabilities)`` for one full check."""
    try:
        endpoints = await asyncio.to_thread(_mail_login, account_id, endpoints, password)
    except MailHostError as exc:
        return endpoints, _STATUS_OF_CAUSE.get(exc.cause, store.UNREACHABLE), exc.host, ()
    endpoints = await _calendars(endpoints, password)
    if endpoints.calendars:
        return endpoints, store.OK, "", ("mail", "calendar")
    return endpoints, store.MAIL_ONLY, "", ("mail",)


async def run_check(account_id: str, endpoints: Endpoints, token: str) -> str:
    """Run one check for the token ``store.begin_check`` issued; returns the status it recorded."""
    credentials = await run_in_db(_credentials, account_id)
    if credentials is None:
        await run_in_db(lambda: store.write(account_id, endpoints, status=store.WAITING, expect_token=token))
        return store.WAITING
    try:
        endpoints, status, host, capabilities = await _outcome(account_id, endpoints, credentials)
    except Exception as exc:  # noqa: BLE001 - a bug must end the check, never leave the row Checking
        log.error("Mail-host check failed inside CORSIA: %s", type(exc).__name__)
        status, host, capabilities = store.UNREACHABLE, endpoints.imap.host, ()
    written = await run_in_db(lambda: store.write(
        account_id, endpoints, status=status, host=host, capabilities=capabilities, expect_token=token,
    ))
    if written:
        emit_lifecycle_event(
            "connect_succeeded" if status in store.CONNECTED_STATUSES else "connect_failed", account_id, "mailhost",
            **({} if status in store.CONNECTED_STATUSES else {"cause": status}),
        )
    return status


def _credentials(account_id: str) -> str | None:
    from infra.database import get_db

    db = get_db()
    try:
        bundle = resolve_credentials(account_id, db=db)
    finally:
        db.close()
    return str(bundle[PASSWORD]) if bundle else None


async def start_check(account_id: str, replacement: Endpoints | None = None) -> bool:
    """Mark the row Checking and check it in the background; False when it is no mail-host row.

    ``replacement`` runs the check against new settings (Edit server), saved with the Checking mark.
    """
    begun = await run_in_db(lambda: store.begin_check(account_id, replacement=replacement))
    if begun is None:
        return False
    task = asyncio.create_task(run_check(account_id, *begun))
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return True


async def start_after_key_write(name: str) -> None:
    """After the person saved a key: for a mail-host key, mark its row Checking and check in the background."""
    account_id = store.account_id_for_key(name)
    if account_id is not None:
        await start_check(account_id)


async def reset_after_key_clear(name: str) -> None:
    """After the person cleared a key: for a mail-host key, the row waits for an app password again."""
    account_id = store.account_id_for_key(name)
    if account_id is not None:
        await run_in_db(store.reset_to_waiting, account_id)


async def settle_after_restart() -> int:
    """At startup, end every check a dead process left Checking as Server unreachable, so Try again shows."""
    return await run_in_db(store.settle_orphaned_checks)


async def wait_for_checks() -> None:
    """Wait for every check started in this process; for tests and the demo."""
    await asyncio.gather(*list(_pending), return_exceptions=True)
