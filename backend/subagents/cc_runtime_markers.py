"""Parent-thread marker messages for Claude Code runtime events."""

from __future__ import annotations

import uuid

from infra.queries import insert_message
from infra.send_broadcast import broadcast_stored_message

from subagents.cc_runtime_state import Broadcast


def persist_subagent_marker(
    parent_thread_id: str,
    child_id: str,
    name: str,
    db,
) -> str:
    """Store a marker before its loop-bound broadcasts are emitted."""
    marker_meta = {
        "type": "subagent_ref",
        "child_thread_id": child_id,
        "name": name,
        "provider": "cc",
    }
    marker_text = f"Spawned subagent: {name}"
    return insert_message(
        parent_thread_id, "system", marker_text, marker_meta,
        msg_id=str(uuid.uuid4()), db=db,
    )


def broadcast_subagent_marker(
    parent_thread_id: str,
    child_id: str,
    name: str,
    bcast: Broadcast,
    marker_id: str,
    db,
) -> None:
    broadcast_stored_message(parent_thread_id, marker_id, db=db, broadcast=bcast)
    bcast(parent_thread_id, {
        "type": "subagent_spawned", "thread_id": parent_thread_id,
        "child_thread_id": child_id, "name": name, "provider": "cc",
    })


def post_subagent_marker(
    parent_thread_id: str,
    child_id: str,
    name: str,
    bcast: Broadcast,
    db,
) -> None:
    marker_id = persist_subagent_marker(parent_thread_id, child_id, name, db)
    broadcast_subagent_marker(parent_thread_id, child_id, name, bcast, marker_id, db)
