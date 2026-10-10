"""Agent singleton state — the shared agent instance and its lock."""

from __future__ import annotations

import asyncio
from typing import Any

__all__ = ["_agent", "_agent_lock"]

_agent: Any = None
_agent_lock = asyncio.Lock()
