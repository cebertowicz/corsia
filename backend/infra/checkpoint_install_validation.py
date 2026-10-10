"""Revalidate a frozen cold cut against the install transaction's durable rows."""
from __future__ import annotations

from core.checkpoint_types import CheckpointRecord
from core.tool_result_debt import thread_tool_debt_through


def cold_install_refusal(
    db, record: CheckpointRecord, previous_floor: CheckpointRecord | None = None,
) -> str | None:
    """Use the renderer's cut gate and durable call pairing before publication.

    The caller holds the checkpoint install write transaction; no declaration
    or settlement can commit between these reads and the installed-status CAS.
    Warm checkpoints keep their existing provider-owned admission contract.
    """
    if record.mode != "cold":
        return None
    from history.projection import render_checkpoint_tail
    from history.types import HistoryBoundary, PortableHistoryError

    try:
        render_checkpoint_tail(
            record.thread_id, db=db, start_exclusive=HistoryBoundary.of_checkpoint(record),
        )
    except PortableHistoryError as exc:
        return f"checkpoint boundary is no longer legal: {exc}"
    debt = thread_tool_debt_through(
        record.thread_id,
        record.through_seq,
        db=db,
        start_exclusive_seq=(
            previous_floor.through_seq if previous_floor is not None else None
        ),
    )
    if debt.unsettled or debt.unpayable:
        return "checkpoint boundary has tool calls without durable results"
    return None
