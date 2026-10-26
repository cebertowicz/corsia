"""Record one model call's usage against the task current when the call begins.

A Turn may span several tasks. Its own binding is written once, at start,
and stays its snapshot (``infra.turn_plan_binding``); the ledger used to copy
that snapshot onto every call's row, so a Turn that finished task A and moved
on to task B billed B's calls to A. Each call now carries the executor's valid
selection captured when the call begins -- the same validity rule a starting
Turn snapshots after ``plans.turn_admission.snapshot_after_admission`` -- and
falls back to the Turn's snapshot only when the executor holds no valid
selection. A delegated child holding none is billed to the step it parked
or was handed (``plans.delegated_usage``). The native loop captures it before
dispatching the provider call (``binding_at_call_start``) and hands it in with
the measurement; an external harness reports a call only once it has happened,
so its binding is read at that first observation, which precedes
any tool the call made. Plans answers the question; ``infra.llm_usage`` only
persists the answer, so ``infra`` still never imports ``plans``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import logging
from typing import Any

from infra import database
from infra.database import write_transaction
from infra.llm_usage import (
    PlanUsagePersistenceError,
    UsageReceiptConflictError,
    _turn_binding,
    record_llm_usage,
)
from infra.turn_usage_receipts import PLAN_FIELDS
from infra.turn_usage_remainder import holds_receipts
from llm.resolution import Harness

log = logging.getLogger(__name__)

def call_binding(thread_id: str, *, db=None) -> dict[str, Any] | None:
    """The plan binding a call made by ``thread_id`` now belongs to, or None."""
    from plans import admission_origin, service

    with service._connection(db) as connection:
        return admission_origin.call_snapshot(thread_id, db=connection)


#: Attribution that could not be read: the row carries no task, and says so,
#: rather than borrowing the Turn's snapshot as if it were known.
UNATTRIBUTED: dict[str, Any] = {field: None for field in PLAN_FIELDS}


def binding_at_call_start(thread_id: str) -> dict[str, Any] | None:
    """Capture the task a call about to be made belongs to.

    Read before the provider is dispatched, so the call is billed to the task
    current when it began even if the Turn moves on before the measurement
    arrives. None means the executor holds no valid selection and is no delegated
    child doing a handed step's work, and the ledger falls back to the Turn's own
    snapshot -- the one legitimate fallback.
    A read that fails is ``UNATTRIBUTED``: the call is still recorded, its
    task fields empty, and the failure is logged; nothing here stops the call.
    """
    try:
        return call_binding(thread_id, db=database.get_db())
    except Exception:
        log.exception(
            "usage_calls: call attribution unreadable for %s; "
            "recording the call unattributed", thread_id,
        )
        return dict(UNATTRIBUTED)


def record_call_usage(
    thread_id: str,
    model: str,
    usage: Mapping[str, Any] | None,
    *,
    source: str,
    turn_id: str | None,
    usage_key: str | None,
    ttft_ms: int | None = None,
    binding: Mapping[str, Any] | None,
    harness: Harness | None = None,
    reasoning_effort: str | None = None,
    _emit_product_event: Callable[..., None] | None = None,
) -> bool:
    """Persist one call's usage against the task named by ``binding``.

    ``binding`` is what ``binding_at_call_start`` captured for this call, or
    ``UNATTRIBUTED`` when that read failed, or None when the caller has no
    single call to bind: the executor held no valid selection (nor a delegated
    child's handed or parked step) at call start, or the measurement is a whole-Turn aggregate (an external harness's
    terminal total). None is the Turn's own snapshot, the ledger's existing
    rule, and is never presented as call-time attribution. This entry does
    not resolve attribution itself: what it records is what the caller knew.
    """
    try:
        record_llm_usage(
            thread_id, model, usage, source=source, ttft_ms=ttft_ms,
            turn_id=turn_id, usage_key=usage_key, binding=binding, harness=harness,
            reasoning_effort=reasoning_effort, _emit_product_event=_emit_product_event,
        )
    except (PlanUsagePersistenceError, UsageReceiptConflictError) as exc:
        # Loud, never blocking: the provider result already exists, and the
        # defect is now durable on the task and visible to the owner. Only a
        # defect that cannot itself be written escapes, as
        # ``PlanUsagePersistenceError``.
        reason = "measurement_conflict" if isinstance(exc, UsageReceiptConflictError) else "persist_failed"
        return _defective(thread_id, binding, turn_id, usage_key, reason, exc)
    except Exception as exc:
        if turn_id is None and not (binding and binding.get("plan_node_id")):
            raise  # best effort with no Turn and no task to mark: loud, as any failed write
        # Whatever failed, the call's receipt is missing and the task says so.
        return _defective(thread_id, binding, turn_id, usage_key, "persist_failed", exc)
    if turn_id is not None and not _measured(usage):
        _report_defect(
            thread_id, binding, turn_id, usage_key,
            reason="measurement_missing", detail="no usable provider measurement",
        )
    if turn_id is not None and binding is not None and binding == UNATTRIBUTED:
        # The row is written with no task, and that is said out loud: the
        # thread's owner sees the call whose attribution could not be read.
        _report_defect(
            thread_id, binding, turn_id, usage_key,
            reason="attribution_unreadable",
            detail="the call's task could not be read; usage recorded unattributed",
        )
        return False
    return True




def _defective(
    thread_id: str, binding: Mapping[str, Any] | None, turn_id: str | None,
    usage_key: str | None, reason: str, exc: Exception,
) -> bool:
    """Record one call's missing receipt as its task's defect; the Turn continues."""
    _report_defect(thread_id, binding, turn_id, usage_key, reason=reason, detail=str(exc))
    log.error(
        "usage_calls: plan-bound usage handoff incomplete for %s turn %s (%s); "
        "turn continues", thread_id, turn_id, usage_key,
    )
    return False


def record_observed_call_usage(
    thread_id: str,
    model: str,
    usage: Mapping[str, Any] | None,
    *,
    source: str,
    turn_id: str | None,
    usage_key: str | None,
    harness: Harness | None = None,
    reasoning_effort: str | None = None,
    _emit_product_event: Callable[..., None] | None = None,
) -> bool:
    """Record one call an external harness has just reported.

    The harness names no call start, only the finished call; that first
    observation precedes any tool the call made, so the task current now is
    the task the call was made for. Captured and recorded in one worker
    turn, so nothing moves between the two.
    """
    return record_call_usage(
        thread_id, model, usage, source=source, turn_id=turn_id, usage_key=usage_key,
        binding=binding_at_call_start(thread_id), harness=harness,
        reasoning_effort=reasoning_effort, _emit_product_event=_emit_product_event,
    )


def record_turn_fallback(
    thread_id: str,
    model: str,
    volume: Mapping[str, Any],
    *,
    source: str,
    turn_id: str | None,
    usage_key: str = "terminal",
    harness: Harness | None = None,
    quiet_if_unmeasured: bool = False,
    reasoning_effort: str | None = None,
    _emit_product_event: Callable[..., None] | None = None,
) -> bool:
    """Record a Turn's whole volume only when none of its calls was receipted.

    An external harness receipts every call it measured as it arrives
    (``record_observed_call_usage``: by the provider's id, or by the call's
    ordinal in the Turn), so a Turn's partial success is receipts plus
    per-call defects, never a total. At the Turn's end the harness reports
    the Turn's whole volume here as the one fallback: written whole when the
    ledger holds no receipt for the Turn -- a harness that reported no call
    -- and not at all when it holds any, so no call is counted twice. A call
    whose receipt was refused is on record as that call's defect and is not
    guessed back in by subtraction. A caller may suppress an unmeasured
    fallback only after it proves the errored Turn produced no model output.
    Any other missing measurement is the Turn's task's defect through the
    same path a call's is.
    """
    if turn_id is not None:
        try:
            if holds_receipts(
                database.get_db(), turn_id, source=source, except_key=usage_key,
            ):
                return True
            if quiet_if_unmeasured and not _measured(volume):
                # The terminal caller proved this error produced no model output,
                # so no provider usage can be missing from the ledger.
                return True
        except Exception as exc:
            return _defective(thread_id, None, turn_id, usage_key, "persist_failed", exc)
    return record_call_usage(
        thread_id, model, volume, source=source, turn_id=turn_id, usage_key=usage_key,
        binding=None, harness=harness, reasoning_effort=reasoning_effort,
        _emit_product_event=_emit_product_event,
    )


def report_unrecordable_call(
    thread_id: str, *, turn_id: str | None, usage_key: str | None, reason: str, detail: str,
) -> None:
    """Put a call whose usage cannot even be attempted on record as a defect.

    A harness that cannot name the model, the provider turn or a measurement
    for a call has nothing the ledger can take; the loss is still a usage
    defect on the Turn's task (the report finds it), never only a log line.
    A report that cannot be written raises ``PlanUsagePersistenceError`` like
    any other.
    """
    _report_defect(thread_id, None, turn_id, usage_key, reason=reason, detail=detail)


def _measured(usage: Mapping[str, Any] | None) -> bool:
    """A usable measurement knows the total, or both parts that make it."""
    if usage is None:
        return False
    if usage.get("total_tokens") is not None:
        return True
    return usage.get("prompt_tokens") is not None and usage.get("completion_tokens") is not None


def _receipt_exists(db, thread_id: str, payload: dict) -> bool:
    """Whether the owner already holds this exact defect's receipt.

    One defect is one (turn, call, reason): usage keys such as
    ``provider-call:1`` repeat on every Turn, so the Turn is part of the key.
    """
    return db.execute(
        """SELECT 1 FROM messages WHERE thread_id=? AND metadata LIKE ?
           AND metadata LIKE ? AND metadata LIKE ? AND metadata LIKE ? LIMIT 1""",
        (
            thread_id, '%"event_type": "usage.recording_failed"%',
            f'%"turn_id": "{payload["turn_id"]}"%',
            f'%"usage_key": "{payload["usage_key"]}"%', f'%"reason": "{payload["reason"]}"%',
        ),
    ).fetchone() is not None


def _report_defect(
    thread_id: str, binding: Mapping[str, Any] | None, turn_id: str | None,
    usage_key: str | None, *, reason: str, detail: str,
) -> None:
    """Make one recording failure visible on the task and to the thread's owner.

    The ledger row is the durable record of the defect (an unknown row is
    counted as unknown by ``commitment_cost``); the task's event and the
    owner's receipt are the notification. The task is the binding's, or the
    Turn's own when the caller named none. A notification that cannot be
    written is not swallowed: it raises ``PlanUsagePersistenceError``, the
    contract error every Turn-bound caller already catches and logs as an
    incomplete handoff, so the Turn continues and the failure is on record.
    The receipt is the idempotency key, so a retried report writes it.
    """
    from infra.queries import insert_message
    from plans import runner_events

    node_id = binding.get("plan_node_id") if binding else None
    payload = {"turn_id": turn_id, "usage_key": usage_key, "reason": reason}
    try:
        with write_transaction(None, scope="report_usage_defect") as db:
            if node_id is None and turn_id is not None:
                # A caller that named no call to bind (a Turn total) or could
                # not read the ledger still has a task: the Turn's own.
                node_id = _turn_binding(db, thread_id, turn_id)["plan_node_id"]
            if node_id is None and reason not in {"attribution_unreadable", "checkpoint_usage_failed"}:
                return  # no task was involved: nothing to mark, nobody to tell
            # Dedupe and both visible effects share the report's transaction.
            if _receipt_exists(db, thread_id, payload):
                return
            if node_id is not None:
                runner_events.record(
                    node_id, "usage_recording_failed", provenance="plans.usage_calls",
                    payload=payload, db=db,
                )
            insert_message(
                thread_id, "system",
                f"Usage recording failed ({reason}): {detail} -- turn {turn_id}, call {usage_key}.",
                metadata={
                    "type": "activity_receipt", "event_type": "usage.recording_failed",
                    "origin": "plan", "plan_node_id": node_id, **payload,
                },
                db=db,
            )
    except Exception as exc:
        log.exception(
            "usage_calls: could not record the usage defect for %s (%s, %s)",
            thread_id, usage_key, reason,
        )
        raise PlanUsagePersistenceError(
            f"usage defect ({reason}) for {usage_key} could not be reported: {exc}"
        ) from exc
