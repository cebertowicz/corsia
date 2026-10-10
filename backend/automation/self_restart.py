"""Durable self-restart operation and global cutoff truth."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

from infra.database import get_db, write_transaction

IN_FLIGHT_TTL_SECONDS = 600
HOLD_PHASES = frozenset({
    "holding",
    "steering",
    "draining",
    "ready",
    "restarting",
    "resuming",
})
ABORTABLE_PHASES = frozenset({
    "holding",
    "steering",
    "draining",
    "ready",
    "restarting",
})
TERMINAL_STATUSES = frozenset({"complete", "failed", "cancelled", "superseded"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _parse(ts: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _row_is_active(row, *, now: datetime | None = None) -> bool:
    """Operational holds never expire; only legacy pre-bounce rows use the TTL."""
    if str(row["status"]) != "in_flight":
        return False
    phase = row["phase"] if "phase" in row.keys() else None
    if phase in HOLD_PHASES:
        return True
    if phase:
        return False
    started = _parse(row["requested_at"])
    if started is None:
        return True
    return ((now or _now()) - started).total_seconds() < IN_FLIGHT_TTL_SECONDS


def _active_row(connection) -> dict | None:
    rows = connection.execute(
        "SELECT * FROM self_restart_requests WHERE status='in_flight' "
        "ORDER BY requested_at DESC,id DESC"
    ).fetchall()
    now = _now()
    for row in rows:
        if _row_is_active(row, now=now):
            return dict(row)
    return None


def in_flight_request(*, db=None) -> dict | None:
    """Return the current durable operation or non-stale legacy guard."""
    connection = db or get_db()
    try:
        return _active_row(connection)
    finally:
        if db is None:
            connection.close()


def operation_by_id(operation_id: str, *, db=None) -> dict | None:
    """Return one durable restart operation, including a terminal row."""
    connection = db or get_db()
    try:
        row = connection.execute(
            "SELECT * FROM self_restart_requests WHERE id=?", (operation_id,)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        if db is None:
            connection.close()


def restart_holds_new_work(*, db=None) -> bool:
    """Return cutoff truth once the durable owner has entered steering.

    Admission is also used by deliberately minimal/legacy database fixtures that
    do not install restart lifecycle state. Such a database cannot contain a
    durable cutoff, so it has no hold to honor; production startup installs the
    owner table before serving ingress. ``holding`` reserves an operation without
    changing the pre-existing build-window ingress behavior.
    """
    connection = db or get_db()
    try:
        installed = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='self_restart_requests'"
        ).fetchone()
        active = _active_row(connection) if installed is not None else None
        return active is not None and active.get("phase") != "holding"
    finally:
        if db is None:
            connection.close()


def try_acquire(
    requester_thread_id: str | None,
    requester_user: str | None,
    from_sha: str | None,
    branch: str | None,
    reason: str | None,
    *,
    operation_id: str | None = None,
    db=None,
    _target_phase: str = "holding",
) -> str | None:
    """Atomically create or confirm one exact caller-owned restart cutoff."""
    if _target_phase not in {"holding", "steering"}:
        raise ValueError(f"invalid restart acquisition phase: {_target_phase}")
    requested_id = operation_id
    operation_id = requested_id or uuid.uuid4().hex
    if requested_id is not None and (
        len(requested_id) != 32
        or any(char not in "0123456789abcdef" for char in requested_id)
    ):
        raise ValueError("operation_id must be 32 lowercase hex characters")
    now = _now_iso()
    with write_transaction(db, scope="restart_operation") as connection:
        active = _active_row(connection)
        if active is not None:
            if (
                requested_id is None
                or str(active["id"]) != requested_id
                or active.get("reason") != reason
                or active.get("phase") != "holding"
            ):
                return None
            if _target_phase == "steering":
                connection.execute(
                    "UPDATE self_restart_requests SET phase='steering',updated_at=? "
                    "WHERE id=? AND status='in_flight' AND phase='holding'",
                    (now, operation_id),
                )
            return requested_id
        if connection.execute(
            "SELECT 1 FROM self_restart_requests WHERE id=?", (operation_id,)
        ).fetchone() is not None:
            return None
        connection.execute(
            "INSERT INTO self_restart_requests "
            "(id,requester_thread_id,requester_user,from_sha,branch,reason,"
            "status,phase,requested_at,cutoff_at,updated_at) "
            "VALUES (?,?,?,?,?,?,'in_flight',?,?,?,?)",
            (
                operation_id,
                requester_thread_id,
                requester_user,
                from_sha,
                branch,
                reason,
                _target_phase,
                now,
                now,
                now,
            ),
        )
    return operation_id


def try_acquire_steering(
    requester_thread_id: str | None,
    requester_user: str | None,
    from_sha: str | None,
    branch: str | None,
    reason: str | None,
    *,
    operation_id: str | None = None,
    db=None,
) -> str | None:
    """Atomically acquire the exact cutoff and consume holding for steering."""
    return try_acquire(
        requester_thread_id,
        requester_user,
        from_sha,
        branch,
        reason,
        operation_id=operation_id,
        db=db,
        _target_phase="steering",
    )


def transition_operation(
    operation_id: str,
    *,
    expected: str,
    target: str,
    db=None,
) -> bool:
    """CAS one nonterminal phase without changing terminal audit timestamps."""
    if target not in HOLD_PHASES:
        raise ValueError(f"invalid restart operation phase: {target}")
    with write_transaction(db, scope="restart_operation") as connection:
        cur = connection.execute(
            "UPDATE self_restart_requests SET phase=?,updated_at=? "
            "WHERE id=? AND status='in_flight' AND phase=?",
            (target, _now_iso(), operation_id, expected),
        )
        return cur.rowcount == 1


def adopt_for_resume(*, db=None) -> str | None:
    """Adopt a hold that survived the bounce so this startup can settle it.

    Any pre-bounce phase is adopted, not just ``restarting``: the instance that
    owned those phases is gone, so this startup is the only actor left that can
    carry the operation to an ending. Without this the hold is never advanced,
    ``complete_operation`` can never apply, and because an operational hold
    deliberately never TTL-releases, every thread's ingress stays queued for the
    life of the process.
    """
    with write_transaction(db, scope="restart_operation") as connection:
        row = _active_row(connection)
        if row is None:
            return None
        operation_id = str(row["id"])
        if str(row.get("phase") or "") == "resuming":
            return operation_id
        connection.execute(
            "UPDATE self_restart_requests SET phase='resuming',updated_at=? "
            "WHERE id=? AND status='in_flight'",
            (_now_iso(), operation_id),
        )
        return operation_id


def complete_operation(
    operation_id: str,
    *,
    deployed_sha: str | None = None,
    db=None,
) -> bool:
    """Release one exact operation only after continuity handoff succeeds."""
    with write_transaction(db, scope="restart_operation") as connection:
        cur = connection.execute(
            "UPDATE self_restart_requests SET status='complete',phase=NULL,"
            "settled_at=?,updated_at=?,deployed_sha=?,error=NULL "
            "WHERE id=? AND status='in_flight' AND phase='resuming'",
            (_now_iso(), _now_iso(), deployed_sha, operation_id),
        )
        return cur.rowcount == 1


def abort_operation(
    operation_id: str,
    *,
    error: str,
    cancelled: bool = False,
    db=None,
) -> bool:
    """Release one exact healthy-instance operation through explicit compensation."""
    status = "cancelled" if cancelled else "failed"
    with write_transaction(db, scope="restart_operation") as connection:
        placeholders = ",".join("?" for _phase in ABORTABLE_PHASES)
        cur = connection.execute(
            "UPDATE self_restart_requests SET status=?,phase=NULL,settled_at=?,"
            "updated_at=?,error=? WHERE id=? AND status='in_flight' "
            f"AND phase IN ({placeholders})",
            (
                status,
                _now_iso(),
                _now_iso(),
                error[:500],
                operation_id,
                *sorted(ABORTABLE_PHASES),
            ),
        )
        return cur.rowcount == 1


def clear_stale_in_flight(
    deployed_sha: str | None = None,
    *,
    db=None,
) -> int:
    """Close only legacy pre-lifecycle rows; operational holds are never auto-released."""
    cutoff = (_now() - timedelta(seconds=IN_FLIGHT_TTL_SECONDS)).isoformat()
    with write_transaction(db, scope="restart_operation") as connection:
        cur = connection.execute(
            "UPDATE self_restart_requests SET status='superseded',settled_at=?,"
            "updated_at=?,deployed_sha=? WHERE status='in_flight' AND phase IS NULL "
            "AND requested_at < ?",
            (_now_iso(), _now_iso(), deployed_sha, cutoff),
        )
        return cur.rowcount
