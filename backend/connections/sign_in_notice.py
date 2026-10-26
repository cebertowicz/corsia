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
changes the person's result page or the stored account.

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
        f"{name} sign-in didn't finish: {message or cause}",
        f"{name} sign-in didn't finish",
        {"provider": provider, "outcome": FAILED, "cause": cause, "message": message, "account_id": account_id},
    )


def _log_undelivered(provider: str, account_id: str, thread_id: str, why: str) -> None:
    log.warning("%s sign-in result for account %s not delivered to thread %s: %s", provider, account_id, thread_id, why)


def _queue(
    db: sqlite3.Connection | None, thread_id: str, state: str, provider: str, account_id: str, **ending: str | None,
) -> None:
    """Queue one ending on ``thread_id``; a target nobody can tell is logged, any other fault logged in full.

    Under an open transaction (``db``) the write nests as a savepoint, so a wake that fails
    leaves the caller's own writes in place.
    """
    from events.delivery import enqueue_event_wake

    summary, chip, details = _wake(provider, account_id, **ending)
    try:
        enqueue_event_wake(
            thread_id, SIGN_IN_FINISHED_EVENT, summary, details, chip=chip, message_id=_message_id(state), db=db,
        )
    except (ValueError, SendRejected) as exc:
        _log_undelivered(provider, account_id, thread_id, type(exc).__name__)
    except Exception:
        log.exception("%s sign-in result for account %s: a defect queued no wake for thread %s", provider, account_id, thread_id)


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


async def notify_expired(thread_id: str | None, state: str, provider: str, account_id: str) -> None:
    """Wake ``thread_id`` with a sign-in nobody finished in time: the same facts on every provider."""
    await notify_starting_agent(thread_id, state, provider, account_id, cause=EXPIRED_CAUSE, message=EXPIRED_MESSAGE)
