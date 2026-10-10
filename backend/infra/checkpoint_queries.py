"""Read-only queries over the checkpoint lifecycle table.

`infra.checkpoints` owns every state-mutating CAS transition; these accessors
never write, so they live apart to keep that mutation owner small enough to
read as one unit.
"""

from __future__ import annotations

import sqlite3
from typing import cast

from core.checkpoint_types import (
    CheckpointMode,
    CheckpointRecord,
    CheckpointStatus,
    CheckpointSummary,
    CheckpointTrigger,
)
from infra.checkpoints import checkpoint_record
from infra.database import get_db


_CURRENT_INSTALLED_QUERY = (
    "FROM checkpoints AS c JOIN threads AS s ON s.id=c.thread_id "
    "WHERE c.thread_id=? AND c.history_epoch=s.history_epoch "
    "AND c.status='installed' ORDER BY c.generation_order DESC LIMIT 1"
)
_SUMMARY_COLUMNS = (
    "generation_order,id,trigger,mode,status,source_model,writer_model,error,started_at"
)

def _summary(row: sqlite3.Row) -> CheckpointSummary:
    """Build the status-only shape used by checkpoint readers."""
    return CheckpointSummary(
        generation_order=int(row["generation_order"]),
        id=str(row["id"]),
        trigger=cast(CheckpointTrigger, row["trigger"]),
        mode=cast(CheckpointMode, row["mode"]),
        status=cast(CheckpointStatus, row["status"]),
        source_model=str(row["source_model"]),
        writer_model=str(row["writer_model"]),
        error=row["error"],
        started_at=str(row["started_at"]),
    )


def current_installed(
    thread_id: str,
    *,
    db: sqlite3.Connection | None = None,
) -> CheckpointRecord | None:
    """Read the newest installed row only from the thread's current epoch."""
    connection = db or get_db()
    try:
        row = connection.execute(
            f"SELECT c.* {_CURRENT_INSTALLED_QUERY}", (thread_id,)
        ).fetchone()
        return checkpoint_record(row) if row is not None else None
    finally:
        if db is None:
            connection.close()


def current_installed_through_seq(
    thread_id: str,
    *,
    db: sqlite3.Connection,
) -> int | None:
    """Return the current installed cut without materializing its private payload."""
    row = db.execute(
        f"SELECT c.through_seq {_CURRENT_INSTALLED_QUERY}", (thread_id,)
    ).fetchone()
    return int(row["through_seq"]) if row is not None else None


def read(
    checkpoint_id: str,
    *,
    db: sqlite3.Connection | None = None,
) -> CheckpointRecord | None:
    """Read one exact retained operation."""
    connection = db or get_db()
    try:
        row = connection.execute(
            "SELECT * FROM checkpoints WHERE id=?", (checkpoint_id,)
        ).fetchone()
        return checkpoint_record(row) if row is not None else None
    finally:
        if db is None:
            connection.close()


def active_for_thread(
    thread_id: str,
    *,
    db: sqlite3.Connection | None = None,
) -> CheckpointRecord | None:
    """Read the thread's sole running or captured operation."""
    connection = db or get_db()
    try:
        row = connection.execute(
            "SELECT * FROM checkpoints WHERE thread_id=? "
            "AND status IN ('running','captured') ORDER BY generation_order LIMIT 1",
            (thread_id,),
        ).fetchone()
        return checkpoint_record(row) if row is not None else None
    finally:
        if db is None:
            connection.close()


def latest_for_thread(
    thread_id: str,
    *,
    db: sqlite3.Connection | None = None,
) -> CheckpointSummary | None:
    """Read the one latest checkpoint status without loading retained blobs."""
    connection = db or get_db()
    try:
        row = connection.execute(
            f"SELECT {_SUMMARY_COLUMNS} FROM checkpoints WHERE thread_id=? "
            "ORDER BY generation_order DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
        return _summary(row) if row is not None else None
    finally:
        if db is None:
            connection.close()


def model_switch_for_thread(
    thread_id: str,
    *,
    source_model: str | None,
    cold_source_model: str,
    destination_model: str,
    requested_at: str,
    db: sqlite3.Connection | None = None,
) -> CheckpointSummary | None:
    """Read the newest checkpoint attempt belonging to one model switch."""
    connection = db or get_db()
    try:
        row = connection.execute(
            f"SELECT {_SUMMARY_COLUMNS} FROM checkpoints "
            "WHERE thread_id=? AND trigger='model_switch' AND started_at>=? "
            "AND ((mode='warm' AND ? IS NOT NULL "
            "AND source_model=? AND writer_model=?) "
            "OR (mode='cold' AND source_model=? AND writer_model=?)) "
            "ORDER BY generation_order DESC LIMIT 1",
            (
                thread_id,
                requested_at,
                source_model,
                source_model,
                source_model,
                cold_source_model,
                destination_model,
            ),
        ).fetchone()
        return _summary(row) if row is not None else None
    finally:
        if db is None:
            connection.close()
