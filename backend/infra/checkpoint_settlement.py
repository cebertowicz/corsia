"""The one owner that finishes a checkpoint left running or captured."""

from __future__ import annotations

import sqlite3

from core.checkpoint_types import CheckpointRecord
from infra import checkpoints
from infra.checkpoints import CheckpointTransitionError, _required_record
from infra.database import write_transaction


def settle_nonterminal(
    checkpoint_id: str, *, reconciled_at: str,
    running_error: str = "checkpoint interrupted before capture",
    db: sqlite3.Connection | None = None,
) -> CheckpointRecord:
    """Finish one running or captured checkpoint through its durable owner."""
    with write_transaction(db, scope="checkpoint_reconcile") as connection:
        record = _required_record(connection, checkpoint_id)
        if record.status == "running":
            changed = connection.execute(
                "UPDATE checkpoints SET status='failed',error=?,ended_at=? "
                "WHERE id=? AND status='running'",
                (running_error, reconciled_at, checkpoint_id),
            )
            if changed.rowcount != 1:
                raise CheckpointTransitionError("checkpoint reconciliation lost its running CAS")
            return _required_record(connection, checkpoint_id)
        if record.status == "captured":
            try:
                return checkpoints.install(
                    checkpoint_id, installed_at=reconciled_at, db=connection,
                )
            except CheckpointTransitionError:
                raise
            except Exception as error:
                # install's savepoint rolled back; retain the result as
                # overtaken so no captured row outlives its settlement.
                detail = str(error).strip() or type(error).__name__
                connection.execute(
                    "UPDATE checkpoints SET status='overtaken',error=?,ended_at=? "
                    "WHERE id=? AND status='captured'",
                    (f"checkpoint install failed: {detail}", reconciled_at, checkpoint_id),
                )
                return _required_record(connection, checkpoint_id)
        return record


def reconcile_startup(
    *,
    reconciled_at: str,
    db: sqlite3.Connection | None = None,
) -> tuple[CheckpointRecord, ...]:
    """Finish every durable checkpoint left nonterminal at startup."""
    with write_transaction(db, scope="checkpoint_reconcile") as connection:
        rows = connection.execute(
            "SELECT id FROM checkpoints WHERE status IN ('running','captured') "
            "ORDER BY generation_order"
        ).fetchall()
        return tuple(
            settle_nonterminal(
                str(row["id"]), reconciled_at=reconciled_at, db=connection,
            )
            for row in rows
        )
