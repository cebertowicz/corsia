"""Report a wake that was accepted into a thread's FIFO and then ended failed.

An event or plan wake is accepted first and admitted to a Turn later, so a
refusal at admission (an unavailable pinned model, a checkpoint refusal) leaves
a ``failed`` row after the producer already reported delivery.

The settlement that marks the row ``failed`` calls ``record_failed_wakes`` in
its own transaction, as ``automation.watches`` records an expiry. That writes
either the report into the owner's FIFO or, when no live owner takes it, a
``dead_letter.park`` row, so a crash after the commit cannot lose the report.
The owner is the failed thread's parent or spawner resolved through
``events.resolve.resolve_delivery_failure``, never the failed thread. A report
is itself a wake: if it fails it goes to its own owner, and the chain of
threads it has visited ends the walk by parking.

A producer whose wake was refused before the FIFO accepted it (an unavailable
owner, a rejected send) has no row to settle, so it calls
``record_unaccepted_wake`` in its own settlement instead.

After the commit the owning frame calls ``drive_failed_wake_owners``, which is
best-effort and never raises into the caller.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable

from loguru import logger

WAKE_FAILED_REASON = "wake_failed_after_accept"
REPORT_EVENT = "delivery_failure.wake_failed_after_accept"
REPORT_KEY = "wake_failure_report"
CHAIN_KEY = "wake_failure_chain"


def record_failed_wakes(db: sqlite3.Connection, message_ids: Iterable[str]) -> None:
    """Record, inside the caller's transaction, a report for each failed wake."""
    for message_id in message_ids:
        row = db.execute(
            "SELECT m.thread_id,m.metadata,m.delivery_state,m.delivery_revision,"
            "m.delivery_error,t.parent_thread_id,t.spawned_by "
            "FROM messages m JOIN threads t ON t.id=m.thread_id WHERE m.id=?",
            (message_id,),
        ).fetchone()
        if row is None or row["delivery_state"] != "failed":
            continue
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except ValueError:
            continue
        if not isinstance(metadata, dict) or metadata.get("event_disposition") != "wake":
            continue
        from automation.continuation import execution
        from infra.time import utc_now_iso

        execution.record_failed_accepted_delivery(
            message_id, db=db, now=utc_now_iso(),
        )
        chain = [*metadata.get(CHAIN_KEY, ()), row["thread_id"]]
        failed = {
            "message_id": message_id, "thread_id": row["thread_id"],
            "event": str(metadata.get("event_type") or metadata.get("routed_event") or "wake"),
            "delivery_error": row["delivery_error"], "url": metadata.get("url"),
        }
        _report(db, failed, int(row["delivery_revision"]), _owner(row), chain)


def record_unaccepted_wake(
    db: sqlite3.Connection, thread_id: str, idempotency_key: str, event: str,
) -> None:
    """Record a refusal when the event's exact idempotency key has no FIFO row.

    ``send_thread_input`` deliberately makes an omitted ``input_id`` equal its
    ``idempotency_key``. Continuation delivery supplies that key, so the
    message lookup proves whether this exact wake entered the FIFO; once it
    has, that message's own settlement reports later failures.
    """
    if db.execute("SELECT 1 FROM messages WHERE id=?", (idempotency_key,)).fetchone():
        return
    row = db.execute(
        "SELECT parent_thread_id,spawned_by FROM threads WHERE id=?", (thread_id,),
    ).fetchone()
    failed = {
        "message_id": idempotency_key, "thread_id": thread_id, "event": event,
        "delivery_error": "refused before the thread accepted it", "url": None,
    }
    _report(
        db, failed, 0, _owner(row) if row else None, [thread_id],
        stage="was refused before the thread accepted it",
    )


def _owner(row) -> str | None:
    return next((c for c in (row["parent_thread_id"], row["spawned_by"]) if c), None)


def _report(
    db: sqlite3.Connection, failed: dict, attempt: int, owner: str | None, chain: list[str],
    *, stage: str = "was accepted and then failed at admission",
) -> None:
    from events import dead_letter, delivery
    from events.health_sink import is_health_sink
    from events.resolve import resolve_delivery_failure
    from infra.queries import get_thread, thread_is_available

    sink = resolve_delivery_failure(owner, reason=WAKE_FAILED_REASON, db=db).thread
    if sink and sink not in chain and not is_health_sink(sink) and thread_is_available(
        get_thread(sink, db=db),
    ):
        db.execute("SAVEPOINT failed_wake_report")
        try:
            delivery.enqueue_event_wake(
                sink, REPORT_EVENT,
                f"A wake for thread {failed['thread_id']} {stage}.",
                failed, metadata={REPORT_KEY: True, CHAIN_KEY: chain},
                message_id=f"wake-failed:{failed['message_id']}:{attempt}", db=db,
            )
            db.execute("RELEASE failed_wake_report")
            return
        except Exception:
            logger.exception("{}: report to {} refused; parking", WAKE_FAILED_REASON, sink)
            db.execute("ROLLBACK TO failed_wake_report")
            db.execute("RELEASE failed_wake_report")
    dead_letter.park(
        REPORT_EVENT,
        {**failed, "summary": f"A wake {stage}; no live owner took the report."},
        WAKE_FAILED_REASON, failed["message_id"], db=db,
    )


async def drive_failed_wake_owners(message_ids: Iterable[str]) -> None:
    """After the commit, drive each owner holding a report for ``message_ids``.

    The report row is already durable; this only starts an idle owner and must
    never raise into the frame that called it.
    """
    ids = tuple(message_ids)
    if not ids:
        return
    try:
        from infra.db_async import run_in_db
        from infra.thread_execution import drive_thread

        for owner in await run_in_db(_owners_holding_reports, ids):
            await drive_thread(owner)
    except Exception:
        logger.exception("{}: driving the report owner failed", WAKE_FAILED_REASON)


def _owners_holding_reports(message_ids: tuple[str, ...]) -> list[str]:
    from infra.database import get_db

    owners: list[str] = []
    for message_id in message_ids:
        rows = get_db().execute(
            "SELECT DISTINCT thread_id FROM messages WHERE delivery_state='queued' "
            "AND id>=? AND id<?",
            (f"wake-failed:{message_id}:", f"wake-failed:{message_id};"),
        ).fetchall()
        owners.extend(r["thread_id"] for r in rows if r["thread_id"] not in owners)
    return owners
