"""Route-facing lifecycle seam for stable-id connected apps."""
from __future__ import annotations

from typing import Literal

RuntimeAction = Literal["connect", "reconnect", "remove", "sign_in", "sign_out"]


async def apply_connected_app_runtime(action: RuntimeAction, app_id: str) -> dict[str, object]:
    """Apply one lifecycle action through the canonical MCP runtime."""
    from tools.mcp_runtime import apply_connected_app_runtime as apply

    return await apply(action, app_id)
