"""Tell the agent that started a provider sign-in how it ended.

The sign-in's OAuth callback calls this once it has a result. The result is
one ``account.sign_in_finished`` event on the platform's delivery path, to the
thread recorded on the pending sign-in; a sign-in the person started records
none and sends nothing. The event id is the sign-in's own, so a replayed
callback is delivered once. The account is already stored by then, so a failed
delivery is logged and never changes the person's result page.
"""

from __future__ import annotations

import hashlib
import logging

log = logging.getLogger(__name__)


def sign_in_id(state: str) -> str:
    """The sign-in's id: a digest of its single-use state, which is itself never sent."""
    return hashlib.sha256(state.encode()).hexdigest()[:32]


async def notify_starting_agent(
    thread_id: str | None, state: str, provider: str, account_id: str, *,
    address: str | None = None, cause: str | None = None, message: str | None = None,
) -> None:
    """Wake ``thread_id`` with the outcome: ``address`` on success, ``cause`` (and its text) on failure."""
    if thread_id is None:
        return
    from events.connectors.account import ACCOUNT_CONNECTOR, CONNECTED, FAILED, OWNER_FACT, SIGN_IN_FINISHED_EVENT
    from events.platform import DEAD_LETTERED, deliver_platform_event

    payload = {
        "signin_id": sign_in_id(state), "provider": provider, "account_id": account_id,
        "outcome": FAILED if cause is not None else CONNECTED, "address": address, "cause": cause,
        "message": message, OWNER_FACT: thread_id,
    }
    try:
        deliveries = await deliver_platform_event(ACCOUNT_CONNECTOR, payload, SIGN_IN_FINISHED_EVENT)
    except Exception as exc:
        log.warning(
            "%s sign-in result for account %s not delivered to thread %s: %s",
            provider, account_id, thread_id, type(exc).__name__,
        )
        return
    for delivery in deliveries:
        if delivery.outcome == DEAD_LETTERED:
            log.warning(
                "%s sign-in result for account %s not delivered to thread %s: %s",
                provider, account_id, thread_id, delivery.reason,
            )
