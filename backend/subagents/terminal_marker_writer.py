"""Durable writers for the rows that end a harness Turn.

`insert_terminal_marker` persists a Codex or Claude Code terminal marker,
stamping the error kinds and a failed `result` with their reference.
`record_provider_tool_violation` handles a Codex provider-native tool event: it
marks the live provider context untrusted, writes one `tool_boundary_violation`
row and raises to end the Turn.
"""

from __future__ import annotations

import uuid
from typing import Callable

from infra.db_async import run_in_db
from infra.turn_errors import insert_turn_error, needs_reference
from infra.queries import insert_message
from infra.send_broadcast import broadcast_message_rows


async def record_provider_tool_violation(ctx, ev: dict, is_current) -> None:
    """Persist and publish one fatal provider-native tool violation.

    A failed write raises: the violation must never go unrecorded.
    """
    tool_type = ev.get("tool_type") or "provider-native-tool"
    phase = ev.get("phase") or "observed"
    if not is_current(ctx, ev, label=f"provider-native action ({phase})"):
        return
    ctx.live.provider_context_untrusted = True
    metadata = {
        "codex": True,
        "type": "tool_boundary_violation",
        "event_type": "harness.tool_boundary_violation",
        "harness": "codex",
        "provider": ev.get("provider") or "codex",
        "tool_type": tool_type,
        "phase": phase,
        "provider_tool_id": ev.get("id"),
        "repair_owner": "execution-dispatch",
        "turn_id": getattr(ctx.live.corsia_turn, "turn_id", None),
    }
    content = (
        f"Codex attempted provider-native {tool_type} ({phase}). "
        "CORSIA tools must run through the CORSIA tool bridge. Host "
        "access cannot authorize provider-native tools; retry through "
        "the corresponding CORSIA tool."
    )
    if not ctx.live.tool_boundary_violation_recorded:
        ctx.live.tool_boundary_violation_recorded = True
        message_id = await run_in_db(
            insert_turn_error,
            insert_message,
            ctx.thread_id,
            content,
            metadata,
            turn_id=getattr(ctx.live.corsia_turn, "turn_id", None),
            msg_id=str(uuid.uuid4()),
        )
        ctx.live.tool_boundary_marker_id = message_id
        await broadcast_message_rows(ctx.thread_id, (message_id,), broadcast=ctx.bcast)
    raise RuntimeError("Codex capability boundary violated")


def insert_terminal_marker(
    insert_message_fn: Callable,
    thread_id: str,
    content: str,
    metadata: dict,
    *,
    msg_id: str,
    created_at: str,
    db=None,
) -> str:
    """Persist an ordinary marker or a Turn-ending failure."""
    if needs_reference(metadata):
        return insert_turn_error(
            insert_message_fn, thread_id, content, metadata,
            turn_id=metadata.get("turn_id"), msg_id=msg_id, created_at=created_at, db=db,
        )
    return insert_message_fn(
        thread_id, "system", content, metadata, msg_id=msg_id, created_at=created_at, db=db,
    )
