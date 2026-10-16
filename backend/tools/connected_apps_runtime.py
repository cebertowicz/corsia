"""Route-facing lifecycle seam for connected apps.

The data and route layers call this stable app-id interface. The MCP runtime fills
it after its private-state migration lands; until then the route behavior remains
persisted-but-disconnected.
"""
from __future__ import annotations

from typing import Literal

RuntimeAction = Literal["connect", "reconnect", "remove"]


async def apply_connected_app_runtime(action: RuntimeAction, app_id: str) -> None:
    """Apply one lifecycle action for a stable app id once the runtime supports it."""
    del action, app_id
