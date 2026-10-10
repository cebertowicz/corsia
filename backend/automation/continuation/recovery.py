"""Crash recovery for dispatched continuation triggers."""

from __future__ import annotations

from datetime import datetime, timezone

from automation.continuation import settlement, triggers
from infra.database import TransactionOwnershipError, get_db, write_transaction
from infra.db_async import run_in_db, run_in_db_unless_owned

_REASON_BY_KIND = {
    "t0_liveness": "progress_unchanged",
    "t1_watchdog": "watchdog_due",
    "t2_watch_expiry": "watch_expired",
    "restart_resume": "restart_resume",
    "automation_clock": "schedule_due",
}


def _sources():
    from automation.continuation.runner import default_sources

    return default_sources()


def _input_was_recorded(db, thread_id: str, idempotency_key: str) -> bool:
    """Read the canonical message identity used by continuation delivery."""
    return db.execute(
        "SELECT 1 FROM messages WHERE thread_id=? AND (id=? OR "
        "(json_valid(metadata) AND "
        "json_extract(metadata,'$.send_idempotency_key')=?)) LIMIT 1",
        (thread_id, idempotency_key, idempotency_key),
    ).fetchone() is not None


async def recover_dispatched(
    *,
    db=None,
    find_input=None,
    sources=None,
    now: str | None = None,
) -> dict[str, int]:
    """Reconcile delivery-unknown rows without holding a write across input lookup."""

    read_db = db if db is not None else get_db()
    if read_db.in_transaction:
        raise TransactionOwnershipError("continuation recovery requires a clean connection")
    now = now or datetime.now(timezone.utc).isoformat()
    source_by_kind = {
        source.kind: source for source in (sources or _sources())
    }
    rows = read_db.execute(
        """SELECT id,kind,thread_id,idempotency_key
           FROM continuation_triggers
           WHERE state='dispatched'
           ORDER BY dispatched_at,id"""
    ).fetchall()
    delivered = 0
    retried = 0
    for row in rows:
        source = source_by_kind[row["kind"]]
        recorded = getattr(source, "delivery_recorded", None)
        if callable(recorded):
            delivered_already = bool(recorded(triggers.get(row["id"], db=read_db), db=read_db))
        elif row["idempotency_key"]:
            if find_input is None:
                delivered_already = _input_was_recorded(
                    read_db, row["thread_id"], row["idempotency_key"]
                )
            else:
                delivered_already = (
                    await find_input(row["thread_id"], row["idempotency_key"])
                ) is not None
        else:
            delivered_already = False
        # With db=None the retry writes on a DB worker; a transaction left open here would block it.
        if read_db.in_transaction:
            raise TransactionOwnershipError("continuation lookup returned with an active caller transaction")
        if not delivered_already:
            retried += await run_in_db_unless_owned(db, _retry_dispatch, row["id"], now, db)
            continue
        settled = settlement.settle_claimed(
            row["id"],
            "delivered",
            _REASON_BY_KIND[row["kind"]],
            source,
            db=db,
            now=now,
        )
        delivered += int(settled is not None)
    return {"delivered": delivered, "retried": retried}



def _retry_dispatch(trigger_id: str, now: str, db=None) -> int:
    """Return one undelivered dispatched trigger to pending; 1 when it moved."""
    with write_transaction(db, scope="continuation_recover") as connection:
        return int(triggers.recover_dispatch(trigger_id, delivered=False, db=connection, now=now))


def _retire_orphaned_kinds(now: str) -> int:
    """Cancel triggers whose source kind no longer exists."""
    with write_transaction(None, scope="continuation_retire") as connection:
        return triggers.cancel_retired_kinds(
            {source.kind for source in _sources()}, db=connection, now=now,
        )


async def startup_recover() -> dict:
    """Retire orphaned rows, rearm durable watches, and reconcile dispatched rows."""

    from automation import watches

    if get_db().in_transaction:  # it would hold SQLite's lock against the worker's write
        raise TransactionOwnershipError("continuation startup recovery requires a clean connection")
    now = datetime.now(timezone.utc).isoformat()
    retired = await run_in_db(_retire_orphaned_kinds, now)
    watch_outcome = watches.rearm_on_startup(db=get_db())
    dispatch_outcome = await recover_dispatched()
    return {
        "retired": retired,
        "watches": watch_outcome,
        "dispatches": dispatch_outcome,
    }
