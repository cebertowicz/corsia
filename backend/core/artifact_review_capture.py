"""Durable terminal effects of a document review fork."""

from __future__ import annotations

import logging

from doceditor import review_requests
from infra.database import get_db
from infra.db_async import run_in_db

logger = logging.getLogger(__name__)


class ReviewForkFailure(RuntimeError):
    """A named platform failure, distinct from the configured review limit."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def submitted(request_id: str) -> bool:
    """Whether this request no longer belongs to an executing fork."""
    db = get_db()
    try:
        row = db.execute("SELECT state FROM doc_review_requests WHERE id=?", (request_id,)).fetchone()
        return row is None or row["state"] != "forked"
    finally:
        db.close()


def orphaned_forks(live_fork_ids, before: str) -> list[str]:
    """Only forks predating this process can be orphans of its predecessor."""
    db = get_db()
    try:
        rows = db.execute(
            "SELECT id,fork_id FROM doc_review_requests WHERE state='forked' "
            "AND created_at<? ORDER BY created_at,id", (before,),
        ).fetchall()
        return [str(row["id"]) for row in rows if row["fork_id"] not in live_fork_ids]
    finally:
        db.close()


def _escalate_in_db(request_id: str, reason: str) -> None:
    db = get_db()
    try:
        review_requests.escalate_fork(db, request_id, reason=reason)
    finally:
        db.close()


def failure_reason(error) -> str:
    """Classify without persisting provider output or credential material."""
    from core.fork_codex_control import ForkItemLimitReached
    from core.fork_runtime import ForkStepLimitReached
    from core.fork_failures import ForkBinaryResolutionError, ForkCredentialsUnavailable

    if isinstance(error, ForkItemLimitReached):
        return "completed_item_missing_identity" if error.identity_missing else "limit"
    if isinstance(error, ReviewForkFailure):
        return error.reason
    if isinstance(error, ForkStepLimitReached):
        return "limit"
    if isinstance(error, ForkCredentialsUnavailable):
        return "missing_credentials"
    if isinstance(error, ForkBinaryResolutionError):
        return "binary_resolution"
    return f"provider_error:{type(error).__name__}"


async def review_limit_or_failure_handler(request_id: str, error=None, *, reason: str | None = None) -> None:
    if await run_in_db(submitted, request_id):
        return
    reason = reason or failure_reason(error)
    log = logger.warning if reason == "limit" else logger.error
    log("artifact review ended request_id=%s reason=%s", request_id, reason)
    await run_in_db(_escalate_in_db, request_id, reason)


async def reconcile_startup_reviews(live_fork_ids, *, before: str) -> list[str]:
    """Fail each previous-process fork once and preserve its work in a live task."""
    request_ids = await run_in_db(orphaned_forks, frozenset(live_fork_ids), before)
    for request_id in request_ids:
        await review_limit_or_failure_handler(request_id, reason="process_interrupted")
    return request_ids


async def preserve_on_shutdown(fork_id: str) -> None:
    """Leave request recovery to startup; shutdown must not wake the reviewer."""


def provider_failure(harness: str, detail: str) -> None:
    """A review's provider error cannot count as its successful submission."""
    from core.fork_types import ProviderForkRejected

    raise ProviderForkRejected(detail)


async def review_end_handler(request_id: str, result=None) -> None:
    if not await run_in_db(submitted, request_id):
        await review_limit_or_failure_handler(request_id, reason="review_completed_without_submission")
