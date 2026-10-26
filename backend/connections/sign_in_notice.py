"""Tell the agent that started a provider sign-in how it ended.

The result is one ``account.sign_in_finished`` event on the platform's delivery
path, to the thread recorded on the pending sign-in; a sign-in the person
started records none and sends nothing. The event id is the sign-in's own, so a
replayed callback is delivered once.

A success is delivered inside the transaction that stores the account
(``wake_on_connect``), so the wake exists exactly when the account does, even if
the callback request is cancelled afterwards. A failure stores nothing, so it is
delivered on its own (``notify_starting_agent``). Either way a failed delivery is
logged and never changes the person's result page or the stored account.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3

log = logging.getLogger(__name__)


def sign_in_id(state: str) -> str:
    """The sign-in's id: a digest of its single-use state, which is itself never sent."""
    return hashlib.sha256(state.encode()).hexdigest()[:32]


def _payload(
    thread_id: str, state: str, provider: str, account_id: str,
    address: str | None, cause: str | None, message: str | None,
) -> dict:
    from events.connectors.account import CONNECTED, FAILED, OWNER_FACT

    return {
        "signin_id": sign_in_id(state), "provider": provider, "account_id": account_id,
        "outcome": FAILED if cause is not None else CONNECTED, "address": address, "cause": cause,
        "message": message, OWNER_FACT: thread_id,
    }


def _log_undelivered(provider: str, account_id: str, thread_id: str, why: str | None) -> None:
    log.warning("%s sign-in result for account %s not delivered to thread %s: %s", provider, account_id, thread_id, why)


def _log_dead_letters(provider: str, account_id: str, thread_id: str, deliveries) -> None:  # type: ignore[no-untyped-def]
    from events.platform import DEAD_LETTERED

    for delivery in deliveries:
        if delivery.outcome == DEAD_LETTERED:
            _log_undelivered(provider, account_id, thread_id, delivery.reason)


def wake_on_connect(
    db: sqlite3.Connection, thread_id: str | None, state: str, provider: str, account_id: str, address: str,
) -> None:
    """Deliver the success inside the caller's open write transaction, committed or rolled back with it.

    The delivery nests as a savepoint, so one that fails leaves the account's own writes in place.
    """
    if thread_id is None:
        return
    from events.connectors.account import ACCOUNT_CONNECTOR, SIGN_IN_FINISHED_EVENT
    from events.platform import deliver_platform_event_sync

    payload = _payload(thread_id, state, provider, account_id, address, None, None)
    try:
        deliveries = deliver_platform_event_sync(ACCOUNT_CONNECTOR, payload, SIGN_IN_FINISHED_EVENT, db=db)
    except Exception as exc:
        _log_undelivered(provider, account_id, thread_id, type(exc).__name__)
        return
    _log_dead_letters(provider, account_id, thread_id, deliveries)


async def notify_starting_agent(
    thread_id: str | None, state: str, provider: str, account_id: str, *,
    cause: str, message: str | None = None,
) -> None:
    """Wake ``thread_id`` with a failed sign-in: ``cause`` and its text."""
    if thread_id is None:
        return
    from events.connectors.account import ACCOUNT_CONNECTOR, SIGN_IN_FINISHED_EVENT
    from events.platform import deliver_platform_event

    payload = _payload(thread_id, state, provider, account_id, None, cause, message)
    try:
        deliveries = await deliver_platform_event(ACCOUNT_CONNECTOR, payload, SIGN_IN_FINISHED_EVENT)
    except Exception as exc:
        _log_undelivered(provider, account_id, thread_id, type(exc).__name__)
        return
    _log_dead_letters(provider, account_id, thread_id, deliveries)
