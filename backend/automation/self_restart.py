"""Durable self-restart operation and global cutoff truth."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from statistics import median
import uuid
import json

from automation.restart_rule import ACTIVE_PHASES, HOLD_PHASES, IN_FLIGHT_TTL_SECONDS
from automation.restart_rule import parse as _parse
from automation.restart_rule import row_is_active
from automation.restart_tokens import revoke_lifecycle_token_for_terminal_operation
from infra.database import get_db, write_transaction

# This is the sole transition policy, including startup adoption and terminals.
TRANSITIONS = {
    None: {"resuming"},  # Legacy pre-lifecycle requests adopted by startup.
    "holding": {"steering", "resuming", "failed", "cancelled"},
    "steering": {"draining", "resuming", "failed", "cancelled"},
    "draining": {"ready", "resuming", "failed", "cancelled"},
    "ready": {"restarting", "resuming", "failed", "cancelled"},
    "restarting": {"resuming", "failed", "cancelled"},
    "resuming": {"verifying", "complete", "failed"},
    "verifying": {"complete", "failed"},
}
TERMINAL_STATUSES = frozenset({"complete", "failed", "cancelled", "superseded"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _row_is_active(row, *, now: datetime | None = None) -> bool:
    return row_is_active(row, now=now or _now())


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


def operation_detail(operation: dict) -> dict:
    """Read structured operation detail without treating missing context as proof."""
    # The column holds verification context in flight, failure context once terminal.
    try:
        detail = json.loads(operation.get("failure_detail") or "{}")
    except (TypeError, ValueError):
        return {}
    return detail if isinstance(detail, dict) else {}


def previous_version(operation: dict) -> str | None:
    """Return the pre-update version captured by the running packaged backend."""
    version = operation_detail(operation).get("previous_version")
    return version if isinstance(version, str) and version else None


def record_drain_total(operation_id: str, total: int, *, db=None) -> bool:
    """Persist the exact number of Turns present when steering began."""
    with write_transaction(db, scope="restart_operation") as connection:
        cur = connection.execute(
            "UPDATE self_restart_requests SET drain_total=?,updated_at=? "
            "WHERE id=? AND status='in_flight'",
            (total, _now_iso(), operation_id),
        )
        return cur.rowcount == 1


def latest_update_operation(*, db=None) -> dict | None:
    """Return the latest non-superseded durable Update operation."""
    connection = db or get_db()
    try:
        row = connection.execute(
            "SELECT * FROM self_restart_requests WHERE kind='update' "
            "AND status != 'superseded' ORDER BY requested_at DESC,id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        if db is None:
            connection.close()


def usual_update_seconds(*, db=None) -> int | None:
    """Return the median duration of the ten most recent completed updates."""
    connection = db or get_db()
    try:
        rows = connection.execute(
            "SELECT requested_at,settled_at FROM self_restart_requests "
            "WHERE kind='update' AND status='complete' "
            "ORDER BY requested_at DESC,id DESC LIMIT 10"
        ).fetchall()
        durations = [
            (settled - requested).total_seconds()
            for row in rows
            if (requested := _parse(row["requested_at"])) is not None
            and (settled := _parse(row["settled_at"])) is not None
            and settled >= requested
        ]
        return int(median(durations)) if durations else None
    finally:
        if db is None:
            connection.close()


def mark_conductor_told(operation_id: str, *, db=None) -> bool:
    """Record exactly one successful corsia-agent notification."""
    with write_transaction(db, scope="restart_operation") as connection:
        cur = connection.execute(
            "UPDATE self_restart_requests SET conductor_told_at=?,updated_at=? "
            "WHERE id=? AND status='failed' AND kind='update' "
            "AND conductor_told_at IS NULL",
            (_now_iso(), _now_iso(), operation_id),
        )
        return cur.rowcount == 1


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
        return active is not None and active.get("phase") in HOLD_PHASES - {"holding"}
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
    kind: str | None = None,
    trigger: str | None = None,
    target_version: str | None = None,
    changes: str | None = None,
    previous_version: str | None = None,
    operation_id: str | None = None,
    claim: Callable[[object], bool] | None = None,
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
        if claim is not None and not claim(connection):
            return None
        detail = {}
        if previous_version:
            detail["previous_version"] = previous_version
        connection.execute(
            "INSERT INTO self_restart_requests "
            "(id,requester_thread_id,requester_user,from_sha,branch,reason,"
            "kind,trigger,target_version,changes,failure_detail,status,phase,requested_at,cutoff_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,'in_flight',?,?,?,?)",
            (
                operation_id,
                requester_thread_id,
                requester_user,
                from_sha,
                branch,
                reason,
                kind,
                trigger,
                target_version,
                changes,
                json.dumps(detail) if detail else None,
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
    expected: str | None,
    target: str,
    db=None,
) -> bool:
    """CAS one nonterminal phase without changing terminal audit timestamps."""
    if target not in ACTIVE_PHASES:
        raise ValueError(f"invalid restart operation phase: {target}")
    if target not in TRANSITIONS.get(expected, ()):
        return False
    with write_transaction(db, scope="restart_operation") as connection:
        cur = connection.execute(
            "UPDATE self_restart_requests SET phase=?,updated_at=? "
            "WHERE id=? AND status='in_flight' AND phase IS ?",
            (target, _now_iso(), operation_id, expected),
        )
        return cur.rowcount == 1


def adopt_for_resume(*, db=None) -> str | None:
    """Adopt a hold that survived the bounce so this startup can settle it.

    Adopt any pre-bounce phase: its owner is gone and operational holds do not
    expire. Otherwise startup cannot complete it and ingress stays queued.
    """
    with write_transaction(db, scope="restart_operation") as connection:
        row = _active_row(connection)
        if row is None:
            return None
        operation_id = str(row["id"])
        phase = row.get("phase")
        if phase in {"resuming", "verifying"}:
            return operation_id
        if not transition_operation(operation_id, expected=phase, target="resuming", db=connection):
            return None
        return operation_id


def complete_operation(
    operation_id: str,
    *,
    deployed_sha: str | None = None,
    failure_detail: str | None = None,
    db=None,
) -> bool:
    """Release one exact operation only after continuity handoff succeeds."""
    phases = sorted(phase for phase, moves in TRANSITIONS.items() if phase is not None and "complete" in moves)
    with write_transaction(db, scope="restart_operation") as connection:
        detail = None if failure_detail is None else _merged_detail(connection, operation_id, failure_detail)
        cur = connection.execute(
            "UPDATE self_restart_requests SET status='complete',phase=NULL,"
            "settled_at=?,updated_at=?,deployed_sha=?,error=NULL,failure_detail=COALESCE(?,failure_detail) "
            "WHERE id=? AND status='in_flight' "
            f"AND phase IN ({','.join('?' for _ in phases)})",
            (_now_iso(), _now_iso(), deployed_sha, detail, operation_id, *phases),
        )
        settled = cur.rowcount == 1
        if settled:
            revoke_lifecycle_token_for_terminal_operation(connection, operation_id)
        return settled


def _merged_detail(connection, operation_id: str, failure_detail: str | None) -> str:
    """The operation's stored detail with ``failure_detail``'s keys laid over it."""
    return json.dumps({**operation_detail(operation_by_id(operation_id, db=connection) or {}),
                       **operation_detail({"failure_detail": failure_detail})})


def abort_operation(
    operation_id: str,
    *,
    error: str,
    cancelled: bool = False,
    failure_code: str | None = None,
    failure_detail: str | None = None,
    db=None,
) -> bool:
    """Release one exact operation through its state-owned terminal transition."""
    status = "cancelled" if cancelled else "failed"
    phases = sorted(phase for phase, moves in TRANSITIONS.items() if phase is not None and status in moves)
    with write_transaction(db, scope="restart_operation") as connection:
        placeholders = ",".join("?" for _phase in phases)
        cur = connection.execute(
            "UPDATE self_restart_requests SET status=?,phase=NULL,settled_at=?,"
            "updated_at=?,error=?,failure_code=?,failure_detail=? "
            "WHERE id=? AND status='in_flight' "
            f"AND phase IN ({placeholders})",
            (
                status,
                _now_iso(),
                _now_iso(),
                error[:500],
                failure_code,
                _merged_detail(connection, operation_id, failure_detail),
                operation_id,
                *phases,
            ),
        )
        settled = cur.rowcount == 1
        if settled:
            revoke_lifecycle_token_for_terminal_operation(connection, operation_id)
        return settled


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
