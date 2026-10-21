"""The one admission path for a GitHub delivery, whatever its source.

``POST /webhooks/github`` calls this after its signature check, and the
notifications poller calls it for each comment it reads from the feed. Both
get the same durable delivery record, delivery- and object-level dedupe,
drain admission, mapping, routing and status updates.

The transaction that records a delivery also writes its raw outcome facts
(``github_outcomes``): the delivery row and its facts commit together or not
at all, and a payload the mapper or the store refuses is counted
``mapping_failed`` in that same transaction. Faults are split by cause, not
swallowed: a refused payload (a field that is missing or the wrong shape) still
gets its delivery row, while a fault the mapper or the store does not document as
a refusal propagates. A mapper fault propagates before the transaction opens and
a store fault aborts it, so either way nothing is written and the route answers
``500``. Neither answer brings the
delivery back on its own: GitHub does not redeliver automatically, and an
operator can redeliver a delivery from the past three days whether it
succeeded or failed. What the ``500`` changes is our own receipt. A ``200``
leaves a delivery row whose dedupe answers every later copy of that id
``duplicate``, so facts lost to a rolled-back savepoint stay lost; with no
row, a redelivery or a re-send is admitted. The notifications poller
does re-read on its own when admission raises, because its cursor only moves
past a comment whose delivery row exists — but the poller only ever delivers
comments, and comments write no outcome facts, so that path cannot bring a
lost fact back.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from events.connectors import github_outcomes
from events.connectors.github_outcome_input import PayloadRefusal
from infra import outcome_events, webhook_delivery_log
from infra.outcome_event_input import OutcomeInputRefusal
from infra.product_events import emit_product_event

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AdmissionOutcome:
    """The HTTP-shaped result of admitting one delivery."""

    status_code: int
    content: dict[str, Any]


def _map_raw_facts(
    event_type: str, payload: dict[str, Any], delivery_id: str,
) -> list[github_outcomes.OutcomeDraft] | None:
    """The delivery's raw drafts, or None when its payload is refused as input.

    Only explicit ``PayloadRefusal`` from consumed-field validation is an input
    problem: it is counted and the delivery is still recorded.
    Incidental exception classes cannot establish cause. Anything else is a defect in
    the mapper or in this delivery, and it propagates here, before the ingress
    transaction opens, so no receipt survives to answer a later copy of the same
    delivery id ``duplicate`` while its facts went unwritten.

    Both logs carry the delivery id and the exception's class name only: never the
    payload, and never the exception text, which can hold a value from it.
    """
    try:
        return github_outcomes.map_github_outcomes(event_type, payload)
    except PayloadRefusal as exc:
        log.warning(
            "GitHub outcome mapping refused delivery %s (%s): %s",
            delivery_id, event_type, type(exc).__name__,
        )
        return None
    except Exception as exc:
        log.error(
            "GitHub outcome mapper fault on delivery %s (%s): %s",
            delivery_id, event_type, type(exc).__name__,
        )
        raise


def _write_raw_facts(
    connection, event_type: str, drafts: list[github_outcomes.OutcomeDraft] | None,
    delivery_id: str,
) -> None:
    """Write one delivery's facts as a unit inside the caller's transaction.

    Only explicit ``OutcomeInputRefusal`` from a deliberate store input guard is
    a refused write. It rolls back every fact of this delivery and is counted once,
    outside the rolled-back savepoint. Its receipt commits with that count, so
    later copies of the same delivery id remain duplicates and cannot recover
    the lost facts. Logs carry the delivery id, never payload or exception text.
    An unexpected fault aborts the caller's whole transaction, including the
    receipt, allowing manual same-id re-entry after recovery. Raw facts carry
    no agent, project or attribution.
    """
    from infra.database import write_transaction

    if drafts:
        try:
            with write_transaction(connection, scope="github_raw_facts"):
                for draft in drafts:
                    outcome_events.record_outcome_event(
                        connection,
                        dedupe_key=draft.dedupe_key,
                        event_name=draft.event_name,
                        source=draft.source,
                        provenance="observed",
                        ts=draft.ts,
                        subject_key=draft.subject_key,
                        attributes=draft.attributes,
                    )
                from core.self_caught_corrections import record_github_outcome_drafts

                record_github_outcome_drafts(connection, drafts)
            return
        except OutcomeInputRefusal as exc:
            # Deliberate credential/budget rejection, never an incidental exception
            # class or a generated-fact/SQL invariant failure.
            log.warning(
                "GitHub outcome facts refused for delivery %s (%s): %s",
                delivery_id, event_type, type(exc).__name__,
            )
    if drafts is None or drafts:
        outcome_events.count_outcome_event(
            connection, "mapping_failed", event_type or "unknown",
        )


def record_delivery_for_admission(
    event_type: str,
    action: str | None,
    delivery_id: str,
    repo: str | None,
    payload: dict[str, Any],
):
    """Linearize cutoff classification, dedup, the initial status and the raw facts."""
    from automation import self_restart
    from infra.database import write_transaction

    if not delivery_id:
        log.warning("Missing X-GitHub-Delivery header — generating synthetic ID")
        delivery_id = f"synthetic-{uuid.uuid4()}"

    # Pure, and it runs before the write lock: an unexpected fault here leaves no
    # delivery row, so the same delivery id can be admitted again.
    drafts = _map_raw_facts(event_type, payload, delivery_id)
    with write_transaction(None, scope="webhook_ingress") as connection:
        held = self_restart.restart_holds_new_work(db=connection)
        record = webhook_delivery_log.record_delivery(
            event_type=event_type,
            action=action,
            delivery_id=delivery_id,
            repo=repo,
            payload=payload,
            status="held_restart" if held else "received",
            db=connection,
        )
        if record.created:
            _write_raw_facts(connection, event_type, drafts, delivery_id)
    return record, delivery_id, held


def _held_outcome(delivery_id: str) -> AdmissionOutcome:
    return AdmissionOutcome(
        202,
        {
            "status": "held",
            "delivery_id": delivery_id,
            "message": (
                "CORSIA is restarting; this delivery was accepted and "
                "will route after healthy startup."
            ),
        },
    )


def _schedule(
    event_type: str, payload: dict[str, Any], delivery_id: str,
) -> str | None | AdmissionOutcome:
    """Schedule the webhook legs, or return the terminal failure outcome.

    The dispatch task gets its own drain admission, released when its legs settle.
    The caller keeps the one it holds, so the drain waits for both.
    """
    from events.connectors.github_webhook import (
        WebhookDispatchPoison,
        schedule_github_webhook,
    )
    from infra.drain import (
        begin_turn_admission,
        finish_turn_admission,
        inherit_turn_admission,
    )

    dispatch = begin_turn_admission()
    internal_event = None
    try:
        with inherit_turn_admission(dispatch):
            internal_event = schedule_github_webhook(
                event_type,
                payload,
                delivery_id=delivery_id,
                on_settled=lambda: finish_turn_admission(dispatch),
            )
        return internal_event
    except WebhookDispatchPoison as exc:
        webhook_delivery_log.update_delivery_status(
            delivery_id, "failed", str(exc), expected_status="received",
        )
        return AdmissionOutcome(422, {"status": "error", "message": str(exc)})
    except Exception as exc:
        webhook_delivery_log.update_delivery_status(
            delivery_id, "failed", str(exc), expected_status="received",
        )
        raise
    finally:
        if internal_event is None:
            finish_turn_admission(dispatch)


def _delivery_metadata(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    """Read metadata inside an object envelope without preempting fact validation."""
    action = payload.get("action")
    repository = payload.get("repository")
    repo = repository.get("full_name") if isinstance(repository, dict) else None
    return action, repo if isinstance(repo, str) else None


def _record_phase(
    event_type: str, payload: dict[str, Any], delivery_id: str, source: str,
) -> tuple[AdmissionOutcome | None, str]:
    """Durably record the delivery; return a terminal outcome, or None to schedule."""
    action, repo = _delivery_metadata(payload)
    emit_product_event(
        "webhook", "received",
        properties={
            "event_type": event_type, "action": action,
            "delivery_id": delivery_id, "repo": repo, "source": source,
        },
    )
    record, delivery_id, held = record_delivery_for_admission(
        event_type, action, delivery_id, repo, payload,
    )
    if not record.created:
        log.info("Duplicate delivery %s — skipping", delivery_id)
        emit_product_event(
            "webhook", "duplicate",
            properties={
                "delivery_id": delivery_id, "event_type": event_type,
                "source": source,
            },
        )
        return AdmissionOutcome(200, {"status": "duplicate", "delivery_id": delivery_id}), delivery_id
    if held:
        return _held_outcome(delivery_id), delivery_id
    return None, delivery_id


def _settle_phase(
    event_type: str, payload: dict[str, Any], delivery_id: str,
    internal_event: str | None, source: str,
) -> AdmissionOutcome:
    """Record whether the scheduled delivery was ignored or dispatched."""
    action, repo = _delivery_metadata(payload)
    if internal_event is None:
        webhook_delivery_log.update_delivery_status(
            delivery_id, "ignored", expected_status="received",
        )
        log.info(
            "Unhandled GitHub event %s/%s from %s — ignoring",
            event_type, action, repo,
        )
        return AdmissionOutcome(
            200, {"status": "ignored", "event_type": event_type, "action": action},
        )
    webhook_delivery_log.update_delivery_status(
        delivery_id, "dispatched", expected_status="received",
    )
    log.info(
        "Routed %s (delivery %s, %s) → owning thread + reviewer (off-loop)",
        internal_event, delivery_id, source,
    )
    return AdmissionOutcome(
        200,
        {
            "status": "ok",
            "event": internal_event,
            "delivery_id": delivery_id,
            "routing": "async",
        },
    )


async def _admission_unit(
    event_type: str,
    payload: dict[str, Any],
    delivery_id: str,
    source: str,
    admission,
    started: list[bool],
) -> AdmissionOutcome:
    """Record, schedule and settle one delivery; holds ``admission`` until it ends."""
    started.append(True)
    from infra.db_async import run_in_db
    from infra.drain import finish_turn_admission

    try:
        outcome, delivery_id = await run_in_db(
            _record_phase, event_type, payload, delivery_id, source,
        )
        if outcome is not None:
            return outcome
        internal_event = _schedule(event_type, payload, delivery_id)
        if isinstance(internal_event, AdmissionOutcome):
            return internal_event
        return await run_in_db(
            _settle_phase, event_type, payload, delivery_id, internal_event, source,
        )
    finally:
        finish_turn_admission(admission)


async def admit_github_delivery_async(
    event_type: str,
    payload: dict[str, Any],
    delivery_id: str,
    *,
    source: str = "webhook",
) -> AdmissionOutcome:
    """Record, dedupe, admit and schedule one parsed GitHub delivery.

    The SQLite phases run off the event loop; scheduling stays on it because it
    creates the dispatch task there. The three phases run as one task that this
    call joins through its own cancellation: a recorded delivery is answered
    ``duplicate`` on redelivery and is never replayed at restart, so a caller
    cancelled after the record commit must not leave it unscheduled.
    """
    import asyncio

    from infra.drain import begin_turn_admission, finish_turn_admission
    from subagents.runtime_guard import join_task_through_cancellation

    admission = begin_turn_admission()
    started: list[bool] = []
    body = _admission_unit(event_type, payload, delivery_id, source, admission, started)
    try:
        unit = asyncio.create_task(body, name=f"github-admission:{delivery_id}")
    except BaseException:
        body.close()
        finish_turn_admission(admission)
        raise
    cancellation = await join_task_through_cancellation(unit)
    if not started:
        finish_turn_admission(admission)
    if unit.cancelled():
        raise cancellation or asyncio.CancelledError()
    if cancellation is not None:
        error = unit.exception()
        if error is not None:
            log.error(
                "GitHub admission of %s failed while its caller was cancelled: %s",
                delivery_id, type(error).__name__,
            )
        raise cancellation
    return unit.result()
