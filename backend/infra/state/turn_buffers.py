"""In-memory accumulator for in-flight turn output."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from core.turn_core import TurnIdentity

MAX_TURN_DURATION_SEC = 15 * 60
SWEEP_INTERVAL_SEC = 60


@dataclass
class TurnBuffer:
    thread_id: str
    turn_id: str
    turn_order: int
    reasoning: str = ""
    content: str = ""
    active_tools: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    seq: int = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "turn_id": self.turn_id,
            "turn_order": self.turn_order,
            "reasoning": self.reasoning,
            "content": self.content,
            "active_tools": [dict(t) for t in self.active_tools],
            "started_at": self.started_at,
            "seq": self.seq,
        }


_buffers: dict[str, TurnBuffer] = {}
_sweeper_task: asyncio.Task | None = None


def init(thread_id: str, identity: TurnIdentity) -> TurnBuffer:
    """Create an empty buffer for a turn."""
    buf = TurnBuffer(
        thread_id=thread_id,
        turn_id=identity.turn_id,
        turn_order=identity.turn_order,
    )
    _buffers[thread_id] = buf
    return buf


def clear(thread_id: str, identity: TurnIdentity) -> None:
    """Remove only the buffer owned by the exact turn. Idempotent."""
    buf = _buffers.get(thread_id)
    if buf is not None and _owns(buf, identity):
        _buffers.pop(thread_id, None)


def _owns(buf: TurnBuffer, identity: TurnIdentity) -> bool:
    return (
        buf.turn_id == identity.turn_id
        and buf.turn_order == identity.turn_order
    )


def get(thread_id: str, identity: TurnIdentity) -> TurnBuffer | None:
    """Return the exact turn's live buffer (not a copy), or None."""
    buf = _buffers.get(thread_id)
    return buf if buf is not None and _owns(buf, identity) else None


def snapshot(thread_id: str, identity: TurnIdentity) -> dict[str, Any] | None:
    """Return the exact turn's serializable snapshot, or None."""
    buf = get(thread_id, identity)
    return buf.snapshot() if buf else None


def append_reasoning(
    thread_id: str, identity: TurnIdentity, chunk: str,
) -> int:
    """Append a reasoning delta. Returns the new seq. 0 if no buffer exists."""
    buf = get(thread_id, identity)
    if not buf or not chunk:
        return 0
    buf.reasoning += chunk
    buf.seq += 1
    return buf.seq


def append_content(thread_id: str, identity: TurnIdentity, chunk: str) -> int:
    """Append a content delta. Returns the new seq. 0 if no buffer exists."""
    buf = get(thread_id, identity)
    if not buf or not chunk:
        return 0
    buf.content += chunk
    buf.seq += 1
    return buf.seq


def set_active_tools(
    thread_id: str,
    identity: TurnIdentity,
    tools: list[dict[str, Any]],
) -> int:
    """Replace the current tool list. Returns the new seq. 0 if no buffer exists."""
    buf = get(thread_id, identity)
    if buf is None:
        return 0
    buf.active_tools = list(tools)
    buf.seq += 1
    return buf.seq


def reset_stream(thread_id: str, identity: TurnIdentity) -> int:
    """Clear reasoning and content while preserving active tools."""
    buf = get(thread_id, identity)
    if buf is None:
        return 0
    buf.reasoning = ""
    buf.content = ""
    buf.seq += 1
    return buf.seq


def finalize_step(
    thread_id: str,
    identity: TurnIdentity,
    finalized_tool_ids: list[str],
) -> int:
    """Reset stream text and consume only tools explicitly finalized."""
    buf = get(thread_id, identity)
    if buf is None:
        return 0
    finalized = set(finalized_tool_ids)
    buf.reasoning = ""
    buf.content = ""
    if finalized:
        buf.active_tools = [
            tool for tool in buf.active_tools
            if tool.get("id") not in finalized
        ]
    buf.seq += 1
    return buf.seq


def active_session_ids() -> list[str]:
    """Snapshot of which sessions currently have a turn buffer."""
    return list(_buffers.keys())


def _sweep_once() -> int:
    """Evict stale buffers that no active turn owns."""
    from infra import state as _state

    now = time.time()
    stale: list[str] = []
    for sid, buf in _buffers.items():
        if now - buf.started_at < MAX_TURN_DURATION_SEC:
            continue
        if sid in getattr(_state, "_active_turns", {}):
            continue
        stale.append(sid)

    for sid in stale:
        _buffers.pop(sid, None)

    if stale:
        logger.warning(
            "turn_buffers: swept {} stale buffer(s): {}",
            len(stale), [s[:8] for s in stale],
        )
    return len(stale)


async def _sweep_loop() -> None:
    try:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SEC)
            try:
                _sweep_once()
            except Exception as e:
                logger.warning("turn_buffers sweep failed: {}", e)
    except asyncio.CancelledError:
        pass


def start_sweeper() -> asyncio.Task:
    """Launch the background sweeper."""
    global _sweeper_task
    if _sweeper_task is not None and not _sweeper_task.done():
        return _sweeper_task
    _sweeper_task = asyncio.create_task(_sweep_loop())
    return _sweeper_task


def stop_sweeper() -> None:
    """Cancel the background sweeper. Safe to call at any time."""
    global _sweeper_task
    if _sweeper_task is not None:
        _sweeper_task.cancel()
        _sweeper_task = None


def _reset_for_tests() -> None:
    _buffers.clear()
    stop_sweeper()
