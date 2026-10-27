"""Tell the agent that started a provider sign-in how it ended.

Each ending is one ``account.sign_in_finished`` input queued on that thread with
``events.delivery.enqueue_event_wake``, the single-target wake the first-run and
restart wakes use. The thread is the one recorded on the pending sign-in; a
sign-in the person started records none and sends nothing. The input id is the
sign-in's own, so a replayed callback is the same input and is kept once.

A success is queued inside the transaction that stores the account
(``wake_on_connect``), so the wake exists exactly when the account does, even if
the callback request is cancelled afterwards. A failure or an expiry stores
nothing, so it is queued in its own write (``notify_starting_agent``,
``notify_expired``). Either way a wake that cannot be queued is logged and never
changes the person's result page or the stored account. A wake the thread refuses
is recorded with ``record_unaccepted_wake``, the platform's one owner for refused
wakes, in the same write.

Once the write has committed, ``drive_starting_agent`` starts the thread: queueing
a wake only writes the FIFO row, as with every other owner wake.

Details never carry a token, code or state: the provider, outcome, address,
cause and a fixed message.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3

from connections.account_state import CONSENT_CANCELLED, PROVIDER_UNAVAILABLE
from infra.db_async import run_in_db
from infra.send_types import SendRejected

log = logging.getLogger(__name__)

SIGN_IN_FINISHED_EVENT = "account.sign_in_finished"
CONNECTED = "connected"
FAILED = "failed"
#: A sign-in nobody finished ends as the cause both providers already give an expired callback.
EXPIRED_CAUSE = CONSENT_CANCELLED
EXPIRED_MESSAGE = "The sign-in link expired before the person finished. Start the sign-in again for a new link."
#: Both providers report a fault inside CORSIA with this one text, whatever the provider.
UNAVAILABLE_MESSAGE = "CORSIA had a problem finishing the sign-in. Start the sign-in again for a new link."
_PROVIDER_NAMES = {"google": "Google", "microsoft": "Microsoft"}


def _message_id(state: str) -> str:
    """The wake's id: a digest of the sign-in's single-use state, which is itself never sent."""
    return f"sign-in:{hashlib.sha256(state.encode()).hexdigest()[:32]}"


def _provider_name(provider: str) -> str:
    return _PROVIDER_NAMES.get(provider, provider.capitalize())


def _wake(
    provider: str, account_id: str, *, address: str | None = None, cause: str | None = None,
    message: str | None = None,
) -> tuple[str, str, dict]:
    """``(summary, chip, details)`` for one ending: the one place the facts and their wording are built.

    A fault inside CORSIA reads the same on every provider, so the same cause carries the same facts.
    """
    name = _provider_name(provider)
    if cause is None:
        return (
            f"{name} sign-in finished: {address} is connected.",
            f"{name} sign-in finished",
            {"provider": provider, "outcome": CONNECTED, "address": address, "account_id": account_id},
        )
    if cause == PROVIDER_UNAVAILABLE:
        message = UNAVAILABLE_MESSAGE
    return (
        f"Couldn’t finish {name} sign-in: {message or cause}",
        f"Couldn’t finish {name} sign-in",
        {"provider": provider, "outcome": FAILED, "cause": cause, "message": message, "account_id": account_id},
    )


def _log_undelivered(provider: str, account_id: str, thread_id: str, why: str) -> None:
    log.warning("%s sign-in result for account %s not delivered to thread %s: %s", provider, account_id, thread_id, why)


def _record_refusal(conn: sqlite3.Connection, thread_id: str, message_id: str) -> None:
    """Hand a wake the thread refused to the platform's owner for refused wakes; a fault there is only logged."""
    from infra.database import write_transaction
    from infra.failed_wake_surface import record_unaccepted_wake

    try:
        with write_transaction(conn, scope="sign_in_wake_refused"):
            record_unaccepted_wake(conn, thread_id, message_id, SIGN_IN_FINISHED_EVENT)
    except Exception:
        log.exception("sign-in wake refused by thread %s could not be reported", thread_id)


def _queue(
    db: sqlite3.Connection | None, thread_id: str, state: str, provider: str, account_id: str, **ending: str | None,
) -> None:
    """Queue one ending on ``thread_id``; a wake it refuses is logged and reported, any other fault logged in full.

    Under an open transaction (``db``) the write nests as a savepoint, so a wake that fails
    leaves the caller's own writes in place.
    """
    from events.delivery import enqueue_event_wake
    from infra.database import write_transaction

    summary, chip, details = _wake(provider, account_id, **ending)
    message_id = _message_id(state)
    with write_transaction(db, scope="sign_in_wake") as conn:
        try:
            enqueue_event_wake(
                thread_id, SIGN_IN_FINISHED_EVENT, summary, details, chip=chip, message_id=message_id, db=conn,
            )
        except (ValueError, SendRejected) as exc:
            _log_undelivered(provider, account_id, thread_id, type(exc).__name__)
            _record_refusal(conn, thread_id, message_id)
        except Exception:
            log.exception(
                "%s sign-in result for account %s: a defect queued no wake for thread %s",
                provider, account_id, thread_id,
            )
            _record_refusal(conn, thread_id, message_id)


def _was_queued(message_id: str) -> bool:
    from infra.database import get_db

    return get_db().execute("SELECT 1 FROM messages WHERE id=?", (message_id,)).fetchone() is not None


async def drive_starting_agent(thread_id: str | None, state: str) -> None:
    """After the wake's write has committed, start the thread it queued on, or the owner told it was refused.

    A drive inside the write could not see its input, and a fenced thread is left alone, as
    every other owner wake does (``script_runner._drive_owner``). A drive that fails is logged
    and changes nothing the person sees.
    """
    if thread_id is None:
        return
    from automation.script_runner import _drive_owner
    from infra.failed_wake_surface import drive_failed_wake_owners

    message_id = _message_id(state)
    try:
        if await run_in_db(_was_queued, message_id):
            await _drive_owner(thread_id)
        await drive_failed_wake_owners([message_id])
    except Exception:
        log.exception("driving thread %s after its sign-in wake failed", thread_id)


def wake_on_connect(
    db: sqlite3.Connection, thread_id: str | None, state: str, provider: str, account_id: str, address: str,
) -> None:
    """Queue the success inside the caller's open write transaction, committed or rolled back with it."""
    if thread_id is not None:
        _queue(db, thread_id, state, provider, account_id, address=address)


async def notify_starting_agent(
    thread_id: str | None, state: str, provider: str, account_id: str, *,
    cause: str, message: str | None = None,
) -> None:
    """Wake ``thread_id`` with a sign-in that ended without an account: ``cause`` and its text."""
    if thread_id is not None:
        await run_in_db(_queue, None, thread_id, state, provider, account_id, cause=cause, message=message)
        await drive_starting_agent(thread_id, state)


async def notify_expired(thread_id: str | None, state: str, provider: str, account_id: str) -> None:
    """Wake ``thread_id`` with a sign-in nobody finished in time: the same facts on every provider."""
    await notify_starting_agent(thread_id, state, provider, account_id, cause=EXPIRED_CAUSE, message=EXPIRED_MESSAGE)
