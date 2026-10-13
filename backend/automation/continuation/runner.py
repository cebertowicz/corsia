"""Single durable admission, delivery, and settlement loop."""

from __future__ import annotations

from collections import Counter
from typing import Protocol

from loguru import logger

from automation.continuation import admission, execution, state, triggers
from automation.continuation.sources.feedback_batches import FeedbackBatchSource
from automation.continuation.sources.harvest import HarvestSource
from automation.continuation.sources.liveness import LivenessSource
from automation.continuation.sources.owed_verdict import OwedVerdictSource
from automation.continuation.sources.schedules import ScheduleSource
from automation.continuation.sources.silent_child import SilentChildSource
from automation.continuation.sources.watch_expiry import WatchExpirySource
from automation.continuation.sources.watchdog import WatchdogSource
from infra.database import TransactionOwnershipError, get_db, write_transaction
from infra.db_async import run_in_db_unless_owned
from plans import public


class TriggerSource(Protocol):
    """Source contract shared by every continuation trigger class.

    Three further methods are optional, so they are stated here rather than
    declared as members a source must have. Callers probe for each with
    ``getattr(source, name, None)``, and a source that omits one falls back
    to the runner's shared behavior:

    - ``prepare(now, *, db) -> list[str] | None``: own preparation before
      collection; ``db=None`` declares standalone ownership. Returns the
      threads its committed writes gave a plan wake, if any; the runner
      starts their Turns once preparation has committed.
    - ``async deliver(thread_id, event, summary, details) -> bool``:
      source-owned delivery, overriding the runner's shared deliver.
    - ``delivery_recorded(trigger, *, db) -> bool``: whether a
      crash-interrupted delivery already landed.
    """

    kind: str

    def due(self, now: str, *, db) -> list[triggers.TriggerRequest]: ...

    def settle(
        self,
        trigger: triggers.Trigger,
        outcome: str,
        reason: str,
        *,
        db,
    ) -> tuple[str, str, dict] | None: ...


def default_sources() -> list[TriggerSource]:
    """Return the one source set admitted by the continuation runner."""

    return [
        LivenessSource(),
        WatchdogSource(),
        WatchExpirySource(),
        FeedbackBatchSource(),
        ScheduleSource(),
        HarvestSource(),
        SilentChildSource(),
        OwedVerdictSource(),
    ]


def _prepare(now: str, sources: list[TriggerSource], db) -> list[str]:
    """Run optional source-owned writes before collection opens a transaction.

    Returns the threads those writes gave a plan wake, for the sweep to drive.
    """
    woken: list[str] = []
    shared_db = db if db is not None else get_db()
    for source in sources:
        prepare = getattr(source, "prepare", None)
        if callable(prepare):
            was_clean = not shared_db.in_transaction
            try:
                woken.extend(prepare(now, db=db) or ())
            except Exception:
                logger.exception(
                    "continuation {} source prepare failed", source.kind,
                )
                if was_clean and shared_db.in_transaction:
                    shared_db.rollback()
    return woken


def _collect(
    now: str,
    sources: list[TriggerSource],
    db,
    startup_jitter_seconds: int,
):
    """Derive source requests before atomically enqueueing their triggers."""
    read_db = db if db is not None else get_db()
    requests: list[tuple[triggers.TriggerRequest, TriggerSource]] = []
    for source in sources:
        was_clean = not read_db.in_transaction
        try:
            requests.extend(
                (request, source) for request in source.due(now, db=read_db)
            )
        except Exception:
            logger.exception("continuation {} source due failed", source.kind)
            if was_clean and read_db.in_transaction:
                read_db.rollback()
    rows = []
    with write_transaction(db, scope="continuation_collect") as connection:
        for request, source in requests:
            request = state.with_startup_jitter(
                request,
                now,
                startup_jitter_seconds,
            )
            trigger, _created = triggers.enqueue(request, db=connection, now=now)
            rows.append((trigger, request, source))
    return rows


def _settle_terminal(rows, *, db, now: str, results) -> None:
    for trigger, request, source in rows:
        terminal = (
            request.outcome is not None
            or trigger.state in triggers.TERMINAL_STATES
        )
        if terminal:
            execution.settle_without_delivery(
                trigger,
                request,
                source,
                db=db,
                now=now,
                results=results,
            )


def _due_batch(rows, now: str):
    return [
        trigger
        for trigger, request, _source in rows
        if request.outcome is None
        and trigger.state in {"pending", "deferred"}
        and (trigger.next_attempt_at is None or trigger.next_attempt_at <= now)
    ]


def _thread_is_idle(callback, thread_id: str, db) -> bool:
    if callback is not None:
        return callback(thread_id)
    from automation.plan_liveness_delivery import thread_is_idle

    return thread_is_idle(thread_id, db=db)


async def _run_batch(
    batch,
    by_id,
    *,
    db,
    now_dt,
    is_idle,
    deliver,
    results,
) -> None:
    read_db = db if db is not None else get_db()
    snapshot = state.admission_snapshot(read_db, batch, now_dt)
    admitted_by_scope: Counter[str] = Counter()
    admitted_total = 0
    for trigger in admission.fair_order(batch, snapshot):
        request, source = by_id[trigger.id]
        if not _thread_is_idle(is_idle, trigger.thread_id, read_db):
            await run_in_db_unless_owned(
                db,
                execution.defer_trigger,
                trigger,
                "thread_unavailable",
                now_dt,
                source,
                db=db,
                results=results,
            )
            continue
        decision = admission.decide(
            trigger,
            snapshot,
            now_dt,
            admitted_by_scope=admitted_by_scope,
            admitted_total=admitted_total,
        )
        if decision.action != "admit":
            await run_in_db_unless_owned(
                db,
                execution.defer_trigger,
                trigger,
                decision.reason,
                now_dt,
                source,
                db=db,
                results=results,
            )
            continue
        admitted_by_scope[trigger.scope_id] += 1
        admitted_total += 1
        await execution.deliver_one(
            trigger,
            request,
            source,
            deliver,
            db=db,
            now=now_dt.isoformat(),
            results=results,
        )


async def sweep(
    now_iso: str,
    *,
    db=None,
    sources: list[TriggerSource] | None = None,
    is_idle=None,
    deliver=None,
    startup_jitter_seconds: int = 0,
) -> dict[str, list[str]]:
    """Collect and deliver due triggers without carrying a transaction across awaits."""

    if db is not None and db.in_transaction:
        raise TransactionOwnershipError("continuation sweep requires a clean caller connection")
    if deliver is None:
        from events.delivery import deliver_to_thread

        deliver = deliver_to_thread
    active_sources = sources or default_sources()
    results: dict[str, list[str]] = {
        key: []
        for key in ("woken", "stalled", "cancelled", "errored", "skipped")
    }
    from infra.connection_limit_drive import drive_limit_releases

    # System phase, not a trigger source: release every stored account limit
    # whose reset has passed. It also recovers any release across a restart.
    await drive_limit_releases(state.parse_iso(now_iso))
    await public.drive_threads(
        await run_in_db_unless_owned(db, _prepare, now_iso, active_sources, db),
    )
    rows = await run_in_db_unless_owned(
        db, _collect, now_iso, active_sources, db, startup_jitter_seconds,
    )
    await run_in_db_unless_owned(
        db, _settle_terminal, rows, db=db, now=now_iso, results=results,
    )
    by_id = {
        trigger.id: (request, source)
        for trigger, request, source in rows
    }
    await _run_batch(
        _due_batch(rows, now_iso),
        by_id,
        db=db,
        now_dt=state.parse_iso(now_iso),
        is_idle=is_idle,
        deliver=deliver,
        results=results,
    )
    return results


# The cadence entry point lives in automation.continuation.loop.
