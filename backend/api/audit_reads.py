"""Authenticated, caller-owned audit metric reads."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from infra.audit_reads import AuditReadError, read_audit_metric
from infra.auth import require_user
from infra.db_async import run_in_db

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("/threads/{thread_id}/metrics/{metric}")
async def audit_metric(
    thread_id: str,
    metric: str,
    since: str | None = Query(None),
    until: str | None = Query(None),
    user: dict = Depends(require_user),
) -> dict:
    """Return one fixed metric within the authenticated user's project scope."""
    try:
        return await run_in_db(
            lambda: read_audit_metric(
                thread_id, user["id"], metric, since=since, until=until
            )
        )
    except AuditReadError as exc:
        status = {
            "scope_denied": 403,
            "invalid_metric": 400,
            "invalid_window": 400,
            "result_limit": 413,
            "timeout": 503,
            "unavailable": 503,
        }.get(exc.category, 503)
        raise HTTPException(status, {"category": exc.category, "next": exc.detail}) from exc
