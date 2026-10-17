"""Connected-app lifecycle error mapping to HTTP refusals."""
from __future__ import annotations

from fastapi import HTTPException

from api.connected_app_access import refusal


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
