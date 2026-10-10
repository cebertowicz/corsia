"""The canonical chat-visible artifact-removal event.

One shared write path for every way an artifact leaves a thread's active
view: hidden from the pane, restored, ownership released, or ownership
rehomed away. Reused by the human-facing HTTP path and agent tool calls so
both render identically and share one grouping contract with the frontend.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from infra import state
from infra.database import write_transaction
from infra.send_broadcast import project_input_message, read_input_broadcast_row

_REMOVAL_SUMMARIES = {
    "hidden_from_pane": "Removed {title} from pane",
    "shown_in_pane": "Restored {title} to pane",
    "ownership_released": "Released {title}",
    "ownership_rehomed": "Moved {title} to another thread",
}


def _persist_artifact_removal_event(
    *,
    db,
    thread_id: str,
    artifact_id: str,
    artifact_type: str,
    title: str | None,
    subtype: str,
    actor_kind: str,
    causal_event_id: str | None,
    hidden: bool | None,
    new_owner_thread_id: str | None,
    triage_thread_id: str | None,
) -> dict[str, Any]:
    """Insert the canonical message into the caller-owned transaction."""
    from infra.queries import insert_message
    from infra.time import utc_now_iso

    summary = _REMOVAL_SUMMARIES[subtype].format(title=title or "artifact")
    now = utc_now_iso()
    meta: dict[str, Any] = {
        "type": "artifact_removed",
        "event_type": f"artifact.{subtype}",
        "subtype": subtype,
        "artifact_id": artifact_id,
        "artifact_type": artifact_type,
        "title": title,
        "actor_kind": actor_kind,
        "causal_event_id": causal_event_id,
    }
    if hidden is not None:
        meta["hidden"] = hidden
    if new_owner_thread_id is not None:
        meta["new_owner_thread_id"] = new_owner_thread_id
    if triage_thread_id is not None:
        meta["triage_thread_id"] = triage_thread_id

    mid = insert_message(
        thread_id, "system", summary, meta, db=db, created_at=now,
    )
    row = read_input_broadcast_row(mid, db=db)
    if row is None:
        raise RuntimeError("Artifact removal message was not persisted")
    return {"thread_id": thread_id, "message": project_input_message(row)}


def broadcast_artifact_removal_event(payload: dict[str, Any]) -> bool:
    """Broadcast a committed removal event; persistence remains authoritative."""
    thread_id = payload["thread_id"]
    try:
        state.broadcast_to_thread(thread_id, {
            "type": "message_added",
            "thread_id": thread_id,
            "message": payload["message"],
        })
    except Exception as exc:  # noqa: BLE001 — reconnect reads the durable row
        logger.error(
            "registry: artifact-removal broadcast failed for {}: {}",
            thread_id, exc,
        )
        return False
    return True


def emit_artifact_removal_event(
    *,
    thread_id: str,
    artifact_id: str,
    artifact_type: str,
    title: str | None,
    subtype: str,
    actor_kind: str,
    causal_event_id: str | None = None,
    hidden: bool | None = None,
    new_owner_thread_id: str | None = None,
    triage_thread_id: str | None = None,
    db=None,
) -> dict[str, Any]:
    """Insert and broadcast one canonical artifact-removal event.

    With a database supplied, insertion is part of the caller transaction
    and the returned payload must be broadcast only after that transaction
    commits. Without one, this helper owns a short transaction and broadcasts
    after commit. Insert failures propagate so the state-changing transaction
    can roll back instead of silently diverging. A live broadcast failure is
    reported by broadcast_artifact_removal_event; the committed message is
    authoritative for reconnect and reload.
    """
    if db is not None:
        return _persist_artifact_removal_event(
            db=db,
            thread_id=thread_id,
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            title=title,
            subtype=subtype,
            actor_kind=actor_kind,
            causal_event_id=causal_event_id,
            hidden=hidden,
            new_owner_thread_id=new_owner_thread_id,
            triage_thread_id=triage_thread_id,
        )

    with write_transaction(None, scope="artifact_removal_event") as owned_db:
        payload = _persist_artifact_removal_event(
            db=owned_db,
            thread_id=thread_id,
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            title=title,
            subtype=subtype,
            actor_kind=actor_kind,
            causal_event_id=causal_event_id,
            hidden=hidden,
            new_owner_thread_id=new_owner_thread_id,
            triage_thread_id=triage_thread_id,
        )
    broadcast_artifact_removal_event(payload)
    return payload


def emit_owner_removal(
    db, identity, old_owner: str, subtype: str, removal_event: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Tell the previous owner's chat the artifact left, when the caller asks."""
    if removal_event is None:
        return None
    return emit_artifact_removal_event(
        thread_id=old_owner,
        artifact_id=identity["id"],
        artifact_type=identity["type"],
        title=identity["title"],
        subtype=subtype,
        db=db,
        **removal_event,
    )
