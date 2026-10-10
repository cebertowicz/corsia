from __future__ import annotations

import asyncio

from fastapi import WebSocket
from loguru import logger

from core.native_turn_control import cancel_and_join_native_turn
from infra import state


async def cancel_active_turn(ws: WebSocket, thread_id: str) -> None:
    cancel_event = state._active_turns.get(thread_id)
    if cancel_event is None:
        return

    logger.info(f"Mid-turn message for session {thread_id}, cancelling active turn")
    try:
        await cancel_and_join_native_turn(thread_id)
    except asyncio.TimeoutError as exc:
        logger.warning(
            "Timed out waiting for cancelled turn on session {}; refusing successor",
            thread_id,
        )
        raise asyncio.TimeoutError(
            f"active turn on session {thread_id} did not stop"
        ) from exc
    except asyncio.CancelledError as exc:
        raise
    except Exception as exc:
        logger.warning(
            "Cancelled-turn join failed for session {}: {}", thread_id, exc,
        )
        raise RuntimeError(
            f"active turn on session {thread_id} did not stop cleanly"
        ) from exc
