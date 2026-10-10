"""Shared delegation transcript marker helpers."""

from __future__ import annotations

import uuid

from loguru import logger

from infra.send_broadcast import broadcast_message_rows
from infra.database import write_transaction
from infra.db_async import run_in_db
from infra.queries import insert_message
from infra.time import utc_now_iso

_SEND_REF_SEED = "corsia-send-ref:"


async def emit_send_ref_marker(
    sender_thread_id: str,
    target_thread_id: str,
    target_title: str,
    target_kind: str | None,
    *,
    source_input_id: str | None = None,
) -> str | None:
    """Insert and broadcast a sender-side /send breadcrumb marker."""
    marker_meta = {
        "type": "send_ref",
        "target_thread_id": target_thread_id,
        "target_title": target_title,
        "target_kind": target_kind,
    }
    marker_text = f"Sent to {target_title}"
    marker_id = (
        str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{_SEND_REF_SEED}{sender_thread_id}:{target_thread_id}:{source_input_id}",
            )
        )
        if source_input_id
        else str(uuid.uuid4())
    )
    now = utc_now_iso()

    try:
        def _insert_marker() -> bool:
            with write_transaction(
                None, scope="delegation_send_ref_marker"
            ) as db:
                if not db.execute(
                    "SELECT id FROM threads WHERE id = ?", (sender_thread_id,)
                ).fetchone():
                    return False
                if source_input_id and db.execute(
                    "SELECT 1 FROM messages WHERE id=?",
                    (marker_id,),
                ).fetchone() is not None:
                    return False
                insert_message(
                    sender_thread_id,
                    "system",
                    marker_text,
                    marker_meta,
                    msg_id=marker_id,
                    db=db,
                    created_at=now,
                )
                return True

        inserted = await run_in_db(_insert_marker)
        if not inserted:
            return None
        await broadcast_message_rows(sender_thread_id, (marker_id,))
        return marker_id
    except Exception as exc:  # noqa: BLE001 - breadcrumb must not undo delivery
        logger.warning(
            "delegation: sender send_ref marker insert failed for {} -> {}: {}",
            sender_thread_id,
            target_thread_id,
            exc,
        )
        return None
