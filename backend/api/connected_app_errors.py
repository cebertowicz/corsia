"""Connected-app validation errors: a refusal with its agent remedy, and the lifecycle error mapping."""
from __future__ import annotations

from fastapi import HTTPException


def refusal(detail: str, status: int = 422) -> HTTPException:
    """Return a connected-app validation error with an explicit safe agent remedy."""
    headers = {"X-Corsia-Agent-Remedy": detail} if status == 403 else None
    return HTTPException(status, detail, headers=headers)


def app_error(exc: Exception) -> HTTPException:
    """Map a connected-app lifecycle failure to its HTTP refusal."""
    from connections.app_reach import (
        AppNotFoundError,
        AppPendingApprovalError,
        AppProposalExpiredError,
        AppRevisionConflictError,
    )

    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, AppNotFoundError):
        return refusal("connected app not found", 404)
    if isinstance(exc, AppRevisionConflictError):
        return refusal("connected app changed; refresh it and retry", 409)
    if isinstance(exc, AppProposalExpiredError):
        return refusal(str(exc), 409)
    if isinstance(exc, AppPendingApprovalError):
        return refusal("approve or decline the change first", 409)
    return refusal(str(exc))
