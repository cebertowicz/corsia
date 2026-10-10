"""Turn tracking state — active turns and exact running-task handles."""

from __future__ import annotations

import asyncio

__all__ = [
    "_active_turns", "_active_llm_tasks", "_active_turn_tasks",
]

_active_turns: dict[str, asyncio.Event] = {}
_active_llm_tasks: dict[str, asyncio.Task] = {}
_active_turn_tasks: dict[str, asyncio.Task] = {}
