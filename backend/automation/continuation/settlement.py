"""Atomic trigger and source settlement on the runner database."""

from __future__ import annotations

from typing import Protocol

from automation.continuation import triggers
from infra.database import write_transaction
from infra.product_events import emit_product_event


class SettlementSource(Protocol):
    """Source-side durable effects coupled to a trigger outcome."""

    def settle(
        self,
        trigger: triggers.Trigger,
        outcome: str,
        reason: str,
        *,
        db,
    ) -> tuple[str, str, dict] | None: ...



def _emit_source_event(event: tuple[str, str, dict] | None, db) -> None:
    """Emit source metric data only after this frame's real commit."""
    if event is not None and (db is None or not db.in_transaction):
        category, action, properties = event
        emit_product_event(category, action, properties=properties)


def settle_claimed(
    trigger_id: str,
    outcome: str,
    reason: str,
    source: SettlementSource,
    *,
    db,
    now: str,
    failed_wake: str | None = None,
) -> triggers.Trigger | None:
    """Commit one claimed terminal trigger and its source effects together.

    ``failed_wake`` names the event of a wake whose delivery failed; the same
    commit reports it if the owner's FIFO never accepted it.
    """

    with write_transaction(db, scope="continuation_settlement") as connection:
        changed = triggers.settle(
            trigger_id,
            outcome=outcome,
            reason=reason,
            db=connection,
            now=now,
        )
        if not changed:
            return None
        settled = triggers.get(trigger_id, db=connection)
        event = source.settle(settled, outcome, reason, db=connection)
        if failed_wake is not None and settled.idempotency_key:
            from infra.failed_wake_surface import record_unaccepted_wake

            record_unaccepted_wake(
                connection, settled.thread_id, settled.idempotency_key, failed_wake,
            )
    _emit_source_event(event, db)
    return settled


def settle_existing(
    trigger: triggers.Trigger,
    source: SettlementSource,
    *,
    db,
) -> triggers.Trigger:
    """Commit idempotent source effects for an already-terminal trigger."""

    with write_transaction(db, scope="continuation_settlement") as connection:
        event = source.settle(trigger, trigger.state, trigger.reason, db=connection)
    _emit_source_event(event, db)
    return trigger
