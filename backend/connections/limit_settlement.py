"""Settle expired account-limit scopes inside a Turn's terminal transaction.

A Turn that started after a scope's reset and completed proves the account is
usable again: the scope is recorded ``allowed``, which drives its held heads
after the commit. A probe Turn that ended without proving it hands its window
back to the sweep as ``failed``. A settlement that raises never rolls back the
terminal and is never swallowed: it returns ``Failed`` and leaves ``failed``
markers so the sweep keeps driving the held heads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from connections.usage_verdicts import LimitSubject

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Settled:
    released: tuple[LimitSubject, ...]
    retried: tuple[LimitSubject, ...]


@dataclass(frozen=True, slots=True)
class NothingDue:
    """No scope of this model's account had expired by the terminal."""


@dataclass(frozen=True, slots=True)
class Failed:
    """Settlement raised; ``marked`` says whether the sweep still has a driver."""

    error: str
    reason: str | None
    marked: bool


ScopeSettlement = Settled | NothingDue | Failed


def _expired(connection, model: str, settled_at: str, *, account_id: str):
    """The model's account and every expired limit candidate at the terminal."""
    from connections.harness_usage import stored_usage
    from connections.usage_verdicts import effective_verdict, parse_time
    from llm.model_status import harness_connection_for_model

    provider = harness_connection_for_model(model, db=connection)
    usage = stored_usage(provider=provider, account_id=account_id, db=connection) if provider is not None else None
    settled = parse_time(settled_at)
    if provider is None or not isinstance(usage, dict) or settled is None:
        return provider, ()
    return provider, effective_verdict(usage, model, settled).expired


def settle_expired_scope(
    *, model: str, thread_id: str, started_at: str, settled_at: str,
    completed: bool, normalized_refusal: bool, turn_id: str, db,
) -> ScopeSettlement:
    """Settle each expired scope of the Turn's own login in its own savepoint of the terminal."""
    from connections.account_receipts import receipt_is_current
    from connections.usage_verdicts import parse_time
    from infra.database import write_transaction
    from infra.vendor_login import read_turn_receipt

    if normalized_refusal or parse_time(started_at) is None or parse_time(settled_at) is None:
        return NothingDue()
    # Only the login the Turn ran on is settled; a later login on its row is not.
    receipt = read_turn_receipt(turn_id, db=db)
    if not receipt_is_current(receipt, db=db):
        return NothingDue()
    try:
        with write_transaction(db, scope="limit_settlement") as connection:
            provider, expired = _expired(connection, model, settled_at, account_id=receipt.account_id)
            if provider is None or not expired:
                return NothingDue()
            return _settle(
                connection, provider, expired, thread_id=thread_id, started_at=started_at,
                settled_at=settled_at, completed=completed, receipt=receipt,
            )
    except Exception as failure:
        log.exception("post-reset usage settlement failed for thread %s", thread_id)
        error = f"{type(failure).__name__}: {failure}"
        reason, marked = _mark_failed(model, thread_id, settled_at, error, receipt=receipt, db=db)
        return Failed(error, reason, marked)


def _settle(
    connection, provider: str, expired, *, thread_id: str, started_at: str,
    settled_at: str, completed: bool, receipt,
) -> Settled:
    from connections.account_receipts import limit_subject
    from connections.harness_usage import record_allowed, write_release
    from connections.usage_verdicts import parse_time, release_marker, scope_key

    started = parse_time(started_at)
    released, retried = [], []
    for verdict in expired:
        subject = limit_subject(provider, scope_key(verdict.get("scope_model")), receipt=receipt)
        reset, observed = parse_time(verdict.get("resets_at")), parse_time(verdict.get("observed_at"))
        if (
            completed and started is not None and reset is not None and reset <= started
            and (observed is None or observed <= started)
            and verdict.get("source") != "plan_limit_overage_off"
        ):
            record_allowed(
                provider=provider, scope_model=subject.scope_model,
                source="post_reset_turn_completed", receipt=receipt, db=connection,
            )
            released.append(subject)
            continue
        marker = release_marker(verdict) or {}
        # Only this window's own probe hands it back; an unmarked window is already due.
        if marker.get("state") == "started" and marker.get("thread_id") == thread_id:
            write_release(
                subject, str(verdict["resets_at"]), "failed", at=settled_at,
                error="probe turn ended without completing", owner_thread_id=thread_id,
                db=connection,
            )
            retried.append(subject)
    return Settled(tuple(released), tuple(retried))


def _mark_failed(model: str, thread_id: str, settled_at: str, error: str, *, receipt, db) -> tuple[str | None, bool]:
    """Leave each expired scope ``failed`` so the sweep drives its heads (C2).

    It runs in its own savepoint: if it raises too, the terminal still commits
    and the caller fails the prefix closed instead.
    """
    from connections.account_receipts import limit_subject
    from connections.harness_usage import write_release
    from connections.usage_verdicts import scope_key
    from infra.database import write_transaction

    reason = None
    try:
        with write_transaction(db, scope="limit_settlement_marker") as connection:
            provider, expired = _expired(connection, model, settled_at, account_id=receipt.account_id)
            for verdict in expired:
                reason = reason or verdict.get("reason")
                write_release(
                    limit_subject(str(provider), scope_key(verdict.get("scope_model")), receipt=receipt),
                    str(verdict["resets_at"]), "failed", at=settled_at, error=error,
                    owner_thread_id=thread_id, db=connection,
                )
    except Exception:
        log.exception("usage-limit release marker not written for thread %s", thread_id)
        return reason, False
    return reason, True
