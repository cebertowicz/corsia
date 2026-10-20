"""Durable terminal effects of a document review fork."""

from __future__ import annotations

import logging

from doceditor import review_requests
from infra.database import get_db
from infra.db_async import run_in_db


logger = logging.getLogger(__name__)


def submitted(request_id: str) -> bool:
    """Whether the request no longer waits on its fork: answered, cancelled or ended."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT answered_batch_id,cancelled_at,fork_id FROM doc_review_requests WHERE id=?",
            (request_id,),
        ).fetchone()
        return bool(row and (row["answered_batch_id"] or row["cancelled_at"] or not row["fork_id"]))
    finally:
        db.close()


def orphaned_forks(live_fork_ids) -> list[str]:
    """Requests still waiting on a fork this process does not run."""
    db = get_db()
    try:
        rows = db.execute(
            "SELECT id,fork_id FROM doc_review_requests WHERE fork_id IS NOT NULL AND node_id IS NULL"
            " AND cancelled_at IS NULL AND answered_batch_id IS NULL ORDER BY created_at,id",
        ).fetchall()
        return [str(row["id"]) for row in rows if row["fork_id"] not in live_fork_ids]
    finally:
        db.close()


def _escalate_in_db(request_id: str) -> None:
    db = get_db()
    try:
        review_requests.escalate_fork(db, request_id)
    finally:
        db.close()


async def review_limit_or_failure_handler(request_id: str, error=None, *, reason: str | None = None) -> None:
    if error is None:
        logger.warning(
            "artifact review escalation request_id=%s reason=%s",
            request_id,
            reason or "unknown",
        )
    else:
        logger.warning(
            "artifact review escalation request_id=%s exception=%s",
            request_id,
            type(error).__name__,
            exc_info=error,
        )
    await run_in_db(_escalate_in_db, request_id)


async def reconcile_startup_reviews(live_fork_ids) -> list[str]:
    """Escalate, once, every review whose in-memory fork died with the last process."""
    request_ids = await run_in_db(orphaned_forks, frozenset(live_fork_ids))
    for request_id in request_ids:
        await run_in_db(_escalate_in_db, request_id)
    return request_ids


async def review_end_handler(request_id: str, result=None) -> None:
    if not await run_in_db(submitted, request_id):
        await review_limit_or_failure_handler(request_id, reason="review_completed_without_submission")
