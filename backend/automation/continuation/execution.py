"""Delivery and durable settlement mechanics for the one continuation runner."""

from __future__ import annotations

from loguru import logger

from automation.continuation import admission, settlement, triggers
from automation.continuation.models import StaleContinuation
from infra.database import TransactionOwnershipError, write_transaction
from infra.db_async import run_in_db_unless_owned
from infra.product_events import emit_product_event


def event_for(request: triggers.TriggerRequest) -> tuple[str, str, dict]:
    """Build one content-safe continuation delivery envelope."""
    events = {
        "t0_liveness": "plan.liveness_wake",
        "t1_watchdog": "plan.watchdog_wake",
        "t2_watch_expiry": "plan.watch_expired",
        "restart_resume": "restart_resume",
        "automation_clock": "automation.scheduled_wake",
    }
    summaries = {
        "t0_liveness": "Actionable structured plan work remains.",
        "t1_watchdog": "An active plan node exceeded its watchdog budget.",
        # Every wait ends here, a blocker armed as a wait included (stuck
        # plans R6), so the wake names the ways out rather than only the fact.
        "t2_watch_expiry": (
            "A wait on your plan reached its deadline. Read the task, then "
            "resolve it, renew the wait (block_node again with the same reason, "
            "or arm_watch with a new deadline), raise a decision, or close it."
        ),
        "restart_resume": "The instance restarted; resume against the new build.",
        "automation_clock": "A bounded automation schedule is due.",
    }
    details = dict(request.details or {})
    # A source that knows more than its kind names the wake itself: a
    # scheduled task's expiry says which task and why (plans.scheduled_wait).
    summary = details.pop("summary", None) or summaries[request.kind]
    if request.kind == "t0_liveness" and details.get("provider_reset_at"):
        summary = (
            "The provider's reported reset time is due. Re-read your plan, then "
            "resume the selected work if the provider is available."
        )
    details.update(
        {
            "trigger_kind": request.kind,
            "subject_id": request.subject_id,
            "source_transition": request.transition,
            "reason": request.reason,
        }
    )
    return events[request.kind], summary, details


def record_result(
    results: dict[str, list[str]], outcome: str, thread_id: str, *, kind: str,
) -> None:
    """Append one terminal continuation outcome."""
    key = {
        "delivered": "woken",
        "stalled": "stalled",
        "cancelled": "cancelled",
        "failed": "errored",
        "suppressed": "skipped",
    }[outcome]
    results[key].append(thread_id)


def settle_without_delivery(
    trigger,
    request,
    source,
    *,
    db,
    now: str,
    results,
) -> None:
    """Persist a source-suppressed or already-terminal trigger."""

    if trigger.state in triggers.TERMINAL_STATES:
        settlement.settle_existing(trigger, source, db=db)
        record_result(
            results, trigger.state, trigger.thread_id, kind=trigger.kind,
        )
        return
    with write_transaction(db, scope="continuation_claim") as connection:
        if not triggers.claim(
            trigger.id,
            idempotency_key=f"terminal:{trigger.id}",
            db=connection,
            now=now,
        ):
            return
    settled = settlement.settle_claimed(
        trigger.id,
        request.outcome,
        request.reason,
        source,
        db=db,
        now=now,
    )
    if settled is not None:
        record_result(
            results, request.outcome, trigger.thread_id, kind=trigger.kind,
        )


def defer_trigger(trigger, reason, now_dt, source, *, db, results) -> None:
    """Apply bounded deferral or terminate an exhausted trigger.

    A busy owner (``thread_unavailable``) is not a capacity limit: it retries
    at the base interval and never spends the deferral budget, so a long Turn
    cannot stall the wake. The source's staleness check still cancels it.
    """

    now = now_dt.isoformat()
    owner_busy = reason == "thread_unavailable"
    if not owner_busy and trigger.deferrals >= admission.MAX_DEFERRALS:
        with write_transaction(db, scope="continuation_claim") as connection:
            if not triggers.claim(
                trigger.id,
                idempotency_key=f"stalled:{trigger.id}",
                db=connection,
                now=now,
            ):
                return
        settled = settlement.settle_claimed(
            trigger.id,
            "stalled",
            "deferral_exhausted",
            source,
            db=db,
            now=now,
        )
        if settled is not None:
            results["stalled"].append(trigger.thread_id)
        return
    due = admission.deferred_until(
        trigger.id, 1 if owner_busy else trigger.deferrals + 1, now_dt,
    )
    changed = settlement.defer_with_source(
        trigger,
        reason,
        due.isoformat(),
        source,
        db=db,
        now=now,
        count_deferral=not owner_busy,
    )
    if changed:
        results["skipped"].append(trigger.thread_id)


def _claim_for_delivery(trigger, key: str, *, db, now: str) -> bool:
    with write_transaction(db, scope="continuation_claim") as connection:
        return triggers.claim(trigger.id, idempotency_key=key, db=connection, now=now)


async def deliver_one(
    trigger,
    request,
    source,
    deliver,
    *,
    db,
    now: str,
    results,
) -> None:
    """Commit a claim before delivery, then settle outside any callback transaction."""

    from events.delivery import _event_idempotency_key

    if db is not None and db.in_transaction:
        raise TransactionOwnershipError("continuation delivery requires a clean caller connection")
    event, summary, details = event_for(request)
    key = _event_idempotency_key(event, summary, details)
    if not await run_in_db_unless_owned(
        db, _claim_for_delivery, trigger, key, db=db, now=now,
    ):
        return
    resolved_deliver = getattr(source, "deliver", None) or deliver
    try:
        landed = await resolved_deliver(trigger.thread_id, event, summary, details)
    except StaleContinuation:
        outcome, reason = "cancelled", "stale_source_state"
    except Exception as exc:  # noqa: BLE001 - logged, reported, and settled failed
        logger.opt(exception=exc).error(
            "continuation: {} wake for task {} raised during delivery: {}",
            trigger.kind, trigger.thread_id, exc,
        )
        outcome, reason = "failed", "delivery_failed"
    else:
        outcome = "delivered" if landed else "failed"
        reason = request.reason if landed else "delivery_failed"
        if not landed:
            logger.warning(
                "continuation: {} wake for task {} was not accepted into its FIFO; "
                "the trigger is settling as delivery_failed",
                trigger.kind,
                trigger.thread_id,
            )
    if db is not None and db.in_transaction:
        raise TransactionOwnershipError("continuation delivery returned with an active caller transaction")
    settled = await run_in_db_unless_owned(
        db,
        settlement.settle_claimed,
        trigger.id,
        outcome,
        reason,
        source,
        db=db,
        now=now,
        failed_wake=event if outcome == "failed" else None,
    )
    if settled is None:
        return
    if outcome == "failed":
        from infra.failed_wake_surface import drive_failed_wake_owners

        await drive_failed_wake_owners((key,))
    record_result(results, outcome, trigger.thread_id, kind=trigger.kind)
    _emit_outcome(trigger, outcome, reason)


def record_failed_accepted_delivery(idempotency_key: str, *, db, now: str) -> None:
    """Fail the triggers whose accepted wake failed admission, and record each.

    The correction is its own ``continuation.failed`` event naming the state it
    corrects; an earlier ``continuation.delivered`` event is left as emitted.
    """
    for trigger in triggers.record_failed_accepted_delivery(idempotency_key, db=db, now=now):
        _emit_outcome(trigger, "failed", "delivery_failed", corrects=trigger.state)


def _emit_outcome(trigger, outcome: str, reason: str, **extra: str) -> None:
    emit_product_event(
        "continuation",
        outcome,
        properties={
            "trigger_id": trigger.id,
            "kind": trigger.kind,
            "reason": reason,
            "scope_id": trigger.scope_id,
            **extra,
        },
    )
