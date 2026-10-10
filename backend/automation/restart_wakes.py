"""Pre-restart continuation intent, materialized once into the message FIFO."""

from __future__ import annotations

import uuid

from infra.database import get_db, write_transaction
from infra.queries import get_thread, thread_is_available
from infra.time import utc_now_iso

_EVENTS = {
    "restart_resume": (
        "system.restart.resume",
        "Instance restarted — resuming registered work",
        "The instance restarted. Resume the unfinished work registered before "
        "restart. Verify current state before repeating effects.",
    ),
    "restart_recovery": (
        "system.restart.recovery",
        "Instance restarted — recovering interrupted work",
        "The prior Turn ended during restart. Resume from durable context, "
        "verify current state, and do not blindly repeat effects.",
    ),
    "restart_cancelled_continue": (
        "system.restart.cancelled_continue",
        "Restart cancelled — continuing registered work",
        "The pending restart was cancelled. Continue from the safe boundary "
        "on the current instance.",
    ),
}


def _operation_id(db) -> str | None:
    row = db.execute(
        "SELECT id FROM self_restart_requests WHERE status='in_flight' "
        "ORDER BY requested_at DESC,id DESC LIMIT 1"
    ).fetchone()
    return None if row is None else str(row["id"])


_REGISTRATION_SEED = "corsia-restart"
_INPUT_SEED = "corsia-restart-input"


def _registration_id(operation_id: str | None, thread_id: str, turn_id: str) -> str:
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"{_REGISTRATION_SEED}:{operation_id or 'external'}:{thread_id}:{turn_id}",
    ))


def _register(
    thread_id: str,
    turn_id: str,
    *,
    reason: str,
    require_active: bool,
    db=None,
) -> str:
    with write_transaction(db, scope="restart_wake") as connection:
        if require_active and connection.execute(
            "SELECT 1 FROM active_turns WHERE id=? AND thread_id=? "
            "AND status='running'",
            (turn_id, thread_id),
        ).fetchone() is None:
            raise RuntimeError("restart continuation requires the exact active Turn")
        operation_id = _operation_id(connection)
        if operation_id is None and require_active:
            raise RuntimeError("no restart operation accepts continuation registration")
        registration_id = _registration_id(operation_id, thread_id, turn_id)
        connection.execute(
            "INSERT INTO restart_wake_registrations "
            "(id,thread_id,reason,status,registered_at,operation_id,turn_id) "
            "VALUES (?,?,?,'pending',?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "reason=CASE WHEN excluded.reason='restart_recovery' "
            "THEN excluded.reason ELSE restart_wake_registrations.reason END "
            "WHERE restart_wake_registrations.status='pending'",
            (
                registration_id, thread_id, reason, utc_now_iso(),
                operation_id, turn_id,
            ),
        )
    return registration_id


def register_restart_resume(thread_id: str, turn_id: str, *, db=None) -> str:
    """Register one agent-requested continuation for the exact active Turn."""
    return _register(
        thread_id, turn_id, reason="restart_resume", require_active=True, db=db,
    )


def register_hard_recovery(thread_id: str, turn_id: str, *, db=None) -> str:
    """Conserve work before hard-cap termination of one exact Turn."""
    return _register(
        thread_id, turn_id, reason="restart_recovery", require_active=False, db=db,
    )


def _materialize_row(connection, row, *, kind: str) -> str:
    from events.delivery import enqueue_event_wake

    internal_event, summary, guidance = _EVENTS[kind]
    registration_id = str(row["id"])
    thread_id = str(row["thread_id"])
    message_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"{_INPUT_SEED}:{kind}:{registration_id}",
    ))
    enqueue_event_wake(
        thread_id,
        internal_event,
        summary,
        {"guidance": guidance},
        metadata={
            "kind": kind,
            "restart_registration_id": registration_id,
        },
        message_id=message_id,
        created_at=str(row["registered_at"]),
        db=connection,
    )
    connection.execute(
        "UPDATE restart_wake_registrations SET status='woken',drained_at=? "
        "WHERE id=? AND status='pending'",
        (utc_now_iso(), registration_id),
    )
    return thread_id


def materialize_pending_registrations(*, db=None) -> tuple[str, ...]:
    """Move each retained restart intent into the sole message FIFO once."""
    with write_transaction(db, scope="materialize_restart_wakes") as connection:
        rows = connection.execute(
            "SELECT * FROM restart_wake_registrations WHERE status='pending' "
            "ORDER BY registered_at,id"
        ).fetchall()
        threads = []
        for row in rows:
            thread_id = str(row["thread_id"])
            if not thread_is_available(get_thread(thread_id, db=connection)):
                connection.execute(
                    "UPDATE restart_wake_registrations SET status='unresolved',"
                    "drained_at=? WHERE id=? AND status='pending'",
                    (utc_now_iso(), row["id"]),
                )
                continue
            kind = (
                "restart_recovery"
                if str(row["reason"]).startswith("restart_recovery")
                else "restart_resume"
            )
            threads.append(_materialize_row(connection, row, kind=kind))
    return tuple(dict.fromkeys(threads))


def cancel_operation_registrations(
    operation_id: str,
    *,
    cancel_without_wake: set[tuple[str, str]] | None = None,
    db=None,
) -> tuple[str, ...]:
    """Release registered work locally when the instance will not restart."""
    silent = cancel_without_wake or set()
    with write_transaction(db, scope="cancel_restart_wakes") as connection:
        rows = connection.execute(
            "SELECT * FROM restart_wake_registrations WHERE operation_id=? "
            "AND status='pending' ORDER BY registered_at,id",
            (operation_id,),
        ).fetchall()
        threads = []
        for row in rows:
            identity = (str(row["thread_id"]), str(row["turn_id"] or ""))
            if identity in silent:
                connection.execute(
                    "UPDATE restart_wake_registrations SET status='cancelled',"
                    "drained_at=? WHERE id=?",
                    (utc_now_iso(), row["id"]),
                )
                continue
            threads.append(_materialize_row(
                connection, row, kind="restart_cancelled_continue",
            ))
    return tuple(dict.fromkeys(threads))


def pending_registration_count(*, db=None) -> int:
    connection = db or get_db()
    try:
        return int(connection.execute(
            "SELECT COUNT(*) FROM restart_wake_registrations WHERE status='pending'"
        ).fetchone()[0])
    finally:
        if db is None:
            connection.close()
