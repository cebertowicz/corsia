"""Route-facing lifecycle seam for stable-id connected apps."""
from __future__ import annotations

import logging
from typing import Literal

RuntimeAction = Literal["connect", "restart", "reconnect", "remove", "sign_out"]
log = logging.getLogger(__name__)


async def apply_connected_app_runtime(action: RuntimeAction, app_id: str) -> dict[str, object]:
    """Apply one lifecycle action through the canonical MCP runtime."""
    from tools.mcp_runtime import apply_connected_app_runtime as apply

    return await apply(action, app_id)


async def restart_after_saved_write(app_id: str) -> dict[str, object]:
    """Restart the app on a write a person just saved, so the change reaches it now or reads as failed.

    An app that is no longer there or now waits for approval can't restart; that is logged with
    its id and class, never a value, and its current runtime state is returned.
    """
    from connections.app_reach import AppNotFoundError, AppPendingApprovalError
    from tools.mcp_runtime import connected_app_state

    try:
        return await apply_connected_app_runtime("restart", app_id)
    except (AppNotFoundError, AppPendingApprovalError) as error:
        log.warning("saved change not applied: app_id=%s class=%s", app_id, type(error).__name__)
        return connected_app_state(app_id)
