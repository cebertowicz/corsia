"""Structured-plan snapshot handling at the Turn-admission boundary."""

from __future__ import annotations

import logging
import sqlite3

from infra.queued_input_settlement import fail_queued
from infra.thread_input_types import FailedInputBatch

log = logging.getLogger(__name__)


def snapshot_or_failure(
    connection: sqlite3.Connection,
    thread_id: str,
    rows: list[sqlite3.Row],
) -> dict | FailedInputBatch:
    """Return one plan snapshot, or fail the derived FIFO prefix visibly."""

    from plans.public import PlanError, snapshot_after_admission

    try:
        return snapshot_after_admission(thread_id, db=connection) or {
            "plan_id": None,
            "plan_node_id": None,
            "plan_attempt_id": None,
            "plan_commitment_id": None,
            "plan_usage_role": None,
        }
    except PlanError as error:
        detail = str(error).strip() or "unknown plan error"
        visible_error = (
            "CORSIA could not start this turn because its active plan could not "
            f"be reconciled ({detail}). Pause the active plan to continue without "
            "it, or correct the named problem and send a new message."
        )
        message_ids = fail_queued(
            connection, thread_id, rows, error=visible_error,
        )
        log.warning(
            "Turn plan admission failed; queued input failed visibly: "
            "thread_id=%s error=%s",
            thread_id,
            error,
        )
        return FailedInputBatch(thread_id, message_ids, visible_error)
