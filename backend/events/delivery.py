"""Route visible system events through the canonical message FIFO."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import TYPE_CHECKING, Any

from loguru import logger

from automation.continuation.models import StaleContinuation
from infra.input_admission import accept_thread_input
from infra.database import write_transaction
from infra.queries import get_thread, thread_is_available
from infra.send_service import SendRejected, send_thread_input
from infra.send_types import InputReceipt
from registry import models

if TYPE_CHECKING:
    from events.identity import RoutingIdentity


def format_event_input(
    internal_event: str,
    summary: str,
    details: dict[str, Any] | None,
    *,
    notice: str | None = None,
) -> str:
    """Render the exact event content delivered to the model."""
    parts = [f"[EVENT: {internal_event}]", f"Summary: {summary}"]
    if details:
        parts.append(f"Details:\n{json.dumps(details, indent=2)}")
    if notice:
        parts.append(notice)
    return "\n".join(parts)


def _event_metadata(
    internal_event: str,
    summary: str,
    details: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
    chip: str | None = None,
) -> dict[str, Any]:
    """The row's metadata: ``summary`` is the chip's line, never the agent's.

    The agent reads ``summary`` in the content ``format_event_input`` renders.
    A producer whose summary reads badly to a person passes a ``chip`` instead.
    A digest event (``infra.passive_changes._event``) is the other shape: its
    ``summary`` stays the agent's and its ``chip`` is the line. The producer owns
    both; ``contextEventModel.ts`` is the one reader.
    """
    return {
        **(metadata or {}),
        "type": "context_event",
        "event_type": internal_event,
        "event_disposition": "wake",
        "summary": chip or summary,
        "url": (details or {}).get("url"),
        "routed_event": internal_event,
    }


def enqueue_event_wake(
    thread_id: str,
    internal_event: str,
    summary: str,
    details: dict[str, Any] | None = None,
    *,
    metadata: dict[str, Any] | None = None,
    message_id: str,
    chip: str | None = None,
    created_at: str | None = None,
    admit_while_fenced: bool = False,
    db: sqlite3.Connection | None = None,
) -> InputReceipt:
    """Append one visible system event inside a caller-owned transaction."""
    from infra.passive_changes import materialize_pending_digest

    with write_transaction(db, scope="enqueue_event_wake") as connection:
        thread_row = get_thread(thread_id, db=connection)
        if not thread_is_available(thread_row):
            raise ValueError("event wake target is unavailable")
        materialize_pending_digest(
            connection,
            thread_id,
            admit_while_fenced=admit_while_fenced,
        )
        accepted = accept_thread_input(
            thread_id,
            role="system",
            content=format_event_input(internal_event, summary, details),
            intent="apply_now",
            message_id=message_id,
            metadata=_event_metadata(internal_event, summary, details, metadata, chip),
            delivery_options={"native_mode": "event", "attachments": []},
            created_at=created_at,
            admit_while_fenced=admit_while_fenced,
            db=connection,
        )
    return InputReceipt(
        accepted.thread_id,
        accepted.message_id,
        accepted.input_order,
        accepted.revision,
        accepted.created_at,
    )


def start_idle_after_commit(connection: sqlite3.Connection, thread_id: str) -> None:
    """Start an idle recipient's Turn once ``connection``'s write commits.

    For a caller of ``enqueue_event_wake`` that has no drive of its own; a
    running Turn is left to take the wake at its end. Outside a
    ``write_transaction`` this raises, so the delivery fails instead of
    reporting a wake nothing would start.
    """
    from infra.write_effects import after_commit_merged

    after_commit_merged(connection, "event_wake_drive", [thread_id], _submit_idle_drives)


def _submit_idle_drives(thread_ids: list[str]) -> None:
    import infra.app_loop as app_loop

    try:
        app_loop.submit(_drive_idle(thread_ids))
    except app_loop.AppLoopUnavailable:
        # Before startup or after shutdown: startup recovery drives queued heads.
        logger.debug("event wake drive for {} left to startup recovery", thread_ids)


async def _drive_idle(thread_ids: list[str]) -> None:
    from infra.admission_fence import is_fenced
    from infra.thread_execution import drive_idle_thread

    for thread_id in thread_ids:
        if is_fenced(thread_id):
            continue
        try:
            await drive_idle_thread(thread_id)
        except Exception:  # noqa: BLE001 - committed already; logged per thread like every post-commit idle drive
            logger.exception("event wake drive failed for {}", thread_id)


async def deliver_to_thread(
    thread_id: str,
    internal_event: str,
    summary: str,
    details: dict[str, Any] | None = None,
    *,
    delivery_id: str | None = None,
    chip: str | None = None,
    notice: str | None = None,
) -> bool:
    """Accept, visibly project, and drive one routed event exactly once."""
    thread_row = get_thread(thread_id)
    if not thread_is_available(thread_row):
        logger.error("event-routing: owning task {} is unavailable", thread_id)
        return False
    try:
        from automation.continuation.validity import condition_for_event

        await send_thread_input(
            thread_id,
            thread_row,
            format_event_input(internal_event, summary, details, notice=notice),
            role="system",
            metadata=_event_metadata(internal_event, summary, details, None, chip),
            idempotency_key=_event_idempotency_key(
                internal_event, summary, details, delivery_id=delivery_id,
            ),
            native_mode="event",
            continuation=condition_for_event(internal_event, details),
        )
    except SendRejected as exc:
        logger.error(
            "event-routing: delivery to task {} rejected: {}",
            thread_id,
            exc.detail,
        )
        return False
    except StaleContinuation:
        raise
    except Exception as exc:
        logger.exception(
            "event-routing: delivery to task {} failed: {}", thread_id, exc,
        )
        return False
    return True


async def deliver_and_resolve_to_thread(
    thread_id: str,
    identity: RoutingIdentity,
    internal_event: str,
    summary: str,
    details: dict[str, Any] | None = None,
    *,
    delivery_id: str | None = None,
    chip: str | None = None,
    notice: str | None = None,
) -> bool:
    """Deliver one event, then resolve the watches this exact delivery ends.

    The one owner of "deliver, then resolve" for every caller that routes an
    event to a thread: resolving only after a successful delivery, never
    before, so a task never loses its wait without the payload it waited for.
    """
    delivered = await deliver_to_thread(
        thread_id, internal_event, summary, details,
        delivery_id=delivery_id, chip=chip, notice=notice,
    )
    if delivered:
        from infra.db_async import run_in_db
        from plans.public import resolve_delivered_event

        await run_in_db(
            resolve_delivered_event, identity, internal_event, recipient=thread_id,
        )
    return delivered


def _event_idempotency_key(
    internal_event: str,
    summary: str,
    details: dict[str, Any] | None,
    *,
    delivery_id: str | None = None,
) -> str:
    identity: dict[str, Any] = {
        "event": internal_event,
        "summary": summary,
        "details": details or {},
    }
    if delivery_id:
        identity["delivery_id"] = delivery_id
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return "event:" + hashlib.sha256(payload.encode()).hexdigest()


def thread_exists_and_active(thread_id: str, *, db=None) -> bool:
    """Read availability without releasing a supplied caller's transaction."""
    return thread_is_available(get_thread(thread_id, db=db))


def owning_thread_for_identity(
    integration: str,
    type: str,
    external_id: str,
) -> str | None:
    state = models.routing_registrations(integration, type, external_id)
    owner = state.get("owner") or state.get("triage")
    return owner["parent_thread_id"] if owner else None
