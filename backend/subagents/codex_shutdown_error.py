"""Durable Codex error marker for backend shutdown."""

from __future__ import annotations

import logging
import uuid

from infra.turn_errors import insert_turn_error
from infra.queries import insert_message
from llm.error_cause import cause_fields

log = logging.getLogger(__name__)


def record_backend_shutdown_marker(thread_id: str, turn_id: str | None) -> str | None:
    """Write the terminal error that survives a backend shutdown."""
    try:
        return insert_turn_error(
            insert_message, thread_id, "Terminated by backend shutdown.",
            {
                "codex": True, "type": "cc_error",
                "event_type": "harness.error", "harness": "codex",
                **cause_fields("codex", "backend_shutdown"), "shutdown": True,
            },
            turn_id=turn_id, msg_id=str(uuid.uuid4()),
        )
    except Exception as exc:
        log.warning("codex shutdown marker failed for %s: %s", thread_id, exc)
        return None


async def record_and_broadcast_shutdown_marker(thread_id: str, turn_id: str | None) -> None:
    """Publish the shutdown marker at its saved position when one was written."""
    from infra.db_async import run_in_db
    from infra.send_broadcast import broadcast_message_rows

    marker = await run_in_db(record_backend_shutdown_marker, thread_id, turn_id)
    if marker:
        await broadcast_message_rows(thread_id, (marker,))
