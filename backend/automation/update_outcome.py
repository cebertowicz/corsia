"""The Update outcome a client has shown, kept on the durable operation row."""

from __future__ import annotations

import semver

from automation.self_restart import _now_iso
from infra.database import get_db, write_transaction


def mark_outcome_seen(operation_id: str, *, db=None) -> bool | None:
    """Record that a client showed this Update's outcome; the first write wins.

    Returns None for an unknown or unsettled operation, True once the outcome is
    marked seen (whether by this call or an earlier one).
    """
    with write_transaction(db, scope="restart_operation") as connection:
        row = connection.execute(
            "SELECT status FROM self_restart_requests WHERE id=? AND kind='update'",
            (operation_id,),
        ).fetchone()
        if row is None or row["status"] == "in_flight":
            return None
        connection.execute(
            "UPDATE self_restart_requests SET outcome_seen_at=? "
            "WHERE id=? AND outcome_seen_at IS NULL",
            (_now_iso(), operation_id),
        )
        return True


def completed_update_for_installed_version(
    current_version: str | None, target_version: str | None, *, db=None
) -> dict | None:
    """Return the last completed update when a stale offer targets its installed version."""
    if not current_version or not target_version:
        return None
    try:
        current = semver.Version.parse(current_version)
        target = semver.Version.parse(target_version)
    except ValueError:
        return None
    if target > current:
        return None

    from automation.self_restart import latest_update_operation

    operation = latest_update_operation(db=db)
    if operation is None or operation.get("status") != "complete":
        return None
    try:
        operation_target = semver.Version.parse(str(operation.get("target_version") or ""))
    except ValueError:
        return None
    return operation if operation_target == target else None


def staged_update_activated(operation: dict, *, db=None) -> bool:
    """True when a restart finished after this update stopped on a staged build awaiting one.

    Only that failure is cleared this way: a restart loads the staged build,
    but it cannot fix a download, build or checkout failure.
    """
    if operation.get("failure_code") != "restart_pending":
        return False
    since = operation.get("settled_at") or operation.get("requested_at")
    if not since:
        return False
    connection = db or get_db()
    try:
        return connection.execute(
            "SELECT 1 FROM self_restart_requests WHERE status='complete' "
            "AND requested_at >= ? LIMIT 1",
            (since,),
        ).fetchone() is not None
    finally:
        if db is None:
            connection.close()
