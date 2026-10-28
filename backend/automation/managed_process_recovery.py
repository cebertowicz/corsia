"""Reconcile durable managed-process ownership after a backend restart."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Literal, NoReturn

from loguru import logger

from host_access.managed_runtime import read_log_tail

from automation.managed_process_events import (
    apply_transition,
    drain_retirements,
    epoch_iso,
)
from infra.managed_process_store import get_handle, unsettled_handles, upsert_transition

OrphanResult = Literal["stopped", "already_gone", "unverified"]


def _still_ours(row: dict[str, Any], pgid: int) -> bool | None:
    from host_access.processes import ProcessBindingError, _process_creation_fingerprint

    try:
        return _process_creation_fingerprint(pgid) == row["creation_fingerprint"]
    except ProcessBindingError:
        return None


def _reconcile_orphan_group(row: dict[str, Any]) -> OrphanResult:
    from host_access.processes import stop_orphan_group

    pgid = int(row["pgid"] or 0)
    return stop_orphan_group(pgid, lambda: _still_ours(row, pgid))


def read_persisted_logs(
    owner: Any,
    row: dict[str, Any],
    limit_bytes: int,
) -> str:
    """Read a bounded durable log after its live runtime has gone away."""
    long_running = bool(row["long_running"])
    cap = owner.long_running_tail_bytes if long_running else owner.max_log_bytes
    bounded = max(1, min(int(limit_bytes), cap))
    # Found from the handle, not the stored path, which names the folder the
    # state was in when the process started.
    payload = read_log_tail(
        owner.process_dir / f"{row['handle']}.log",
        long_running=long_running,
        limit_bytes=bounded,
    )
    return payload[-bounded:].decode("utf-8", errors="replace")


def _unverified_stop_message(handle: str) -> str:
    return (
        f"managed process {handle} death could not be verified; its handle, log, "
        "and capacity reservation remain. Do not retry in a loop or touch another "
        "thread's work; tell the user now that this thread's capacity is blocked. "
        "A later managed_process_stop is the only supported agent recovery."
    )


def raise_live_stop_error(
    owner: Any,
    handle: str,
    thread_id: str,
    error: Exception,
) -> NoReturn:
    """Add recovery guidance only while the live owner still reserves capacity."""
    from host_access.processes import ProcessCleanupError

    try:
        record = owner.status(handle, thread_id=thread_id)
    except (KeyError, PermissionError):
        raise error
    if record.terminal_state != "death_unverified":
        raise error
    raise ProcessCleanupError(
        f"{error}; {_unverified_stop_message(handle)}"
    ) from error


def stop_persisted_managed_process(
    handle: str,
    thread_id: str,
    *,
    db: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Recover and stop one owned durable handle that has no live owner."""
    from host_access.processes import ProcessCleanupError

    row = get_handle(handle, db=db)
    if row is None:
        raise KeyError("managed process handle is unknown")
    if row["thread_id"] != thread_id:
        raise PermissionError("managed process handle is owned by another thread")
    if row["state"] not in {"running", "death_unverified"}:
        return row
    result = _reconcile_orphan_group(row)
    if result == "unverified":
        unresolved = {
            **row,
            "state": "death_unverified",
            "outcome": "death_unverified",
            "reason": "managed_stop",
        }
        upsert_transition(unresolved, db=db)
        raise ProcessCleanupError(_unverified_stop_message(handle))
    stopped = result == "stopped"
    closed = {
        **row,
        "state": "exited" if stopped else "interrupted_by_restart",
        "outcome": "stopped" if stopped else "interrupted_by_restart",
        "reason": "managed_stop" if stopped else "completion_unknown_after_restart",
        "ended_at": epoch_iso(time.time()) if stopped else None,
    }
    upsert_transition(closed, db=db)
    return closed


def reconcile_managed_processes(
    *, db: sqlite3.Connection | None = None,
) -> tuple[str, ...]:
    """Drain retirements, then settle durable work recorded before restart."""
    drain_retirements()
    threads: list[str] = []
    now = epoch_iso(time.time())
    for row in unsettled_handles(db=db):
        result = _reconcile_orphan_group(row)
        if result == "unverified":
            if row["state"] != "death_unverified":
                upsert_transition(
                    {
                        **row,
                        "state": "death_unverified",
                        "outcome": "death_unverified",
                        "reason": "backend_restart",
                    },
                    db=db,
                )
            continue
        outcome = "interrupted_by_restart"
        reason = (
            "completion_unknown_after_restart"
            if result == "already_gone"
            else "backend_restart"
        )
        closed = {
            **row,
            "state": outcome,
            "outcome": outcome,
            "reason": reason,
            "ended_at": now if result == "stopped" else None,
        }
        logger.info(
            "startup: managed process {} settled as {}", row["handle"], outcome
        )
        thread_id = apply_transition(closed, "terminal", db=db)
        if thread_id is not None:
            threads.append(thread_id)
    return tuple(dict.fromkeys(threads))
