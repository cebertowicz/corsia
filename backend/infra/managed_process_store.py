"""Durable rows for managed background-process handles and retirements."""

from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from infra.database import get_db, write_transaction
from infra.time import utc_now_iso

_COLUMNS = (
    "handle",
    "thread_id",
    "turn_id",
    "lease_id",
    "description",
    "long_running",
    "pid",
    "pgid",
    "creation_fingerprint",
    "log_path",
    "state",
    "outcome",
    "reason",
    "exit_code",
    "started_at",
    "last_output_at",
    "stalled_since",
    "stall_after_seconds",
    "max_lifetime_seconds",
    "ended_at",
    "log_truncated",
)
_UPDATABLE = _COLUMNS[_COLUMNS.index("state") :]
LIST_LIMIT = 20


def upsert_transition(
    row: Mapping[str, Any], *, db: sqlite3.Connection | None = None,
) -> bool:
    """Write one transition unless deletion already staged this handle."""
    values = tuple(row.get(column) for column in _COLUMNS)
    assignments = ",".join(
        f"{column}=excluded.{column}" for column in _UPDATABLE
    )
    with write_transaction(db, scope="managed_process_transition_upsert") as connection:
        retired = connection.execute(
            "SELECT 1 FROM managed_process_retirements WHERE handle=?",
            (row["handle"],),
        ).fetchone()
        if retired is not None:
            return False
        connection.execute(
            f"INSERT INTO managed_process_handles ({','.join(_COLUMNS)}) "
            f"VALUES ({','.join('?' for _ in _COLUMNS)}) "
            f"ON CONFLICT(handle) DO UPDATE SET {assignments}",
            values,
        )
        return True


def record_event_message(
    handle: str, message_id: str, *, db: sqlite3.Connection,
) -> None:
    db.execute(
        "UPDATE managed_process_handles SET event_message_id=? WHERE handle=?",
        (message_id, handle),
    )


def _read(
    sql: str,
    params: tuple[Any, ...],
    db: sqlite3.Connection | None,
) -> list[dict[str, Any]]:
    connection = db or get_db()
    try:
        return [dict(row) for row in connection.execute(sql, params).fetchall()]
    finally:
        if db is None:
            connection.close()


def get_handle(
    handle: str, *, db: sqlite3.Connection | None = None,
) -> dict[str, Any] | None:
    rows = _read(
        "SELECT * FROM managed_process_handles WHERE handle=?", (handle,), db,
    )
    return rows[0] if rows else None


def list_thread_handles(
    thread_id: str,
    *,
    limit: int = LIST_LIMIT,
    db: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    return _read(
        "SELECT * FROM managed_process_handles WHERE thread_id=? "
        "ORDER BY CASE WHEN state IN ('running','death_unverified') "
        "THEN 0 ELSE 1 END, started_at DESC, handle DESC LIMIT ?",
        (thread_id, int(limit)),
        db,
    )


def unsettled_handles(
    *, db: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    return _read(
        "SELECT * FROM managed_process_handles "
        "WHERE state IN ('running','death_unverified') "
        "ORDER BY started_at,handle",
        (),
        db,
    )


def unsettled_family_handles(
    thread_ids: Sequence[str], *, db: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    exact = tuple(dict.fromkeys(thread_ids))
    if not exact:
        return []
    marks = ",".join("?" for _ in exact)
    return _read(
        "SELECT * FROM managed_process_handles "
        f"WHERE thread_id IN ({marks}) AND state IN ('running','death_unverified') "
        "ORDER BY thread_id,handle",
        exact,
        db,
    )


def stage_thread_family_retirements(
    db: sqlite3.Connection,
    thread_ids: Sequence[str],
) -> tuple[str, ...]:
    """Atomically replace exact family handles with immutable cleanup owners.

    A handle's log is ``<handle>.log`` in the current process folder, found
    from the handle alone, so a moved state folder still retires cleanly.
    """
    exact = tuple(dict.fromkeys(thread_ids))
    if not exact:
        return ()
    marks = ",".join("?" for _ in exact)
    rows = db.execute(
        "SELECT handle,thread_id,long_running "
        f"FROM managed_process_handles WHERE thread_id IN ({marks}) "
        "ORDER BY thread_id,handle",
        exact,
    ).fetchall()
    staged_at = utc_now_iso()
    handles: list[str] = []
    for row in rows:
        handle = str(row["handle"])
        head_name = f"{handle}.log"
        if Path(head_name).name != head_name:
            raise ValueError(
                f"managed process {handle} has an unsafe retirement log path"
            )
        tail_name = f"{handle}.tail.log" if bool(row["long_running"]) else None
        db.execute(
            "INSERT OR IGNORE INTO managed_process_retirements "
            "(handle,thread_id,head_log_name,tail_log_name,staged_at) "
            "VALUES (?,?,?,?,?)",
            (handle, str(row["thread_id"]), head_name, tail_name, staged_at),
        )
        handles.append(handle)
    db.execute(
        f"DELETE FROM managed_process_handles WHERE thread_id IN ({marks})",
        exact,
    )
    return tuple(handles)


def list_retirements(
    *, db: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    return _read(
        "SELECT * FROM managed_process_retirements ORDER BY staged_at,handle",
        (),
        db,
    )


def delete_retirement(handle: str, *, db: sqlite3.Connection | None = None) -> None:
    with write_transaction(db, scope="managed_process_retirement_complete") as connection:
        connection.execute(
            "DELETE FROM managed_process_retirements WHERE handle=?", (handle,)
        )
