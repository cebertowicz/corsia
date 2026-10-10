"""Mutation implementation for ``artifact_update``."""

from __future__ import annotations

from typing import Any

from infra.auth import is_owner_sentinel, may_access_artifact
from infra.database import get_db, write_transaction
from infra.queries import get_thread_for_user
from registry import models, pinning
from registry.doc_freshness import mark_verified as mark_document_verified
from registry.removal_events import emit_artifact_removal_event


def _pane_owner_row(
    thread_id: str, user_id: str, artifact_id: str, db,
) -> dict | None:
    """Return the non-sentinel owner row allowed to mutate pane display."""
    if get_thread_for_user(thread_id, user_id, db=db) is None:
        return None
    owner = models.get_artifact(artifact_id, db=db)
    if owner is None or is_owner_sentinel(owner["parent_thread_id"]):
        return None
    return owner


def apply_artifact_update(
    artifact_id: str,
    kwargs: dict[str, Any],
    *,
    actor_thread_id: str,
    user_id: str,
    thread_id: str,
    causal_event_id: str | None,
):
    """Apply one access-checked artifact update and return its broadcast facts."""
    db = get_db()
    try:
        art = models.get_artifact(artifact_id, db=db)
        if art is None or not may_access_artifact(
            art["parent_thread_id"], user_id, db=db
        ):
            return None
        row = art
        if kwargs.get("mark_verified") is True:
            row = mark_document_verified(artifact_id, actor_thread_id, db=db)
        if kwargs.get("description") is not None:
            row = models.update_description(
                artifact_id, kwargs.get("description"), db=db
            )
        if kwargs.get("pinned") is not None:
            owner = _pane_owner_row(thread_id, user_id, artifact_id, db=db)
            if owner is None:
                return None
            row = pinning.update_pinned(
                artifact_id,
                pinned=kwargs.get("pinned"),
                parent_thread_id=thread_id,
                actor_thread_id=actor_thread_id,
                causal_event_id=causal_event_id,
                db=db,
            )
            if row is None:
                return None
        surfacing_changed = False
        event = None
        if kwargs.get("hidden") is not None:
            owner = _pane_owner_row(thread_id, user_id, artifact_id, db=db)
            if owner is None:
                return None
            target_surfacing = (
                models.SURFACING_QUIET
                if kwargs.get("hidden")
                else models.SURFACING_VISIBLE
            )
            surfacing_changed = owner.get("surfacing") != target_surfacing
            with write_transaction(db, scope="artifact_tool_display") as connection:
                row = models.update_surfacing(
                    artifact_id,
                    surfacing=target_surfacing,
                    parent_thread_id=thread_id,
                    actor_thread_id=actor_thread_id,
                    causal_event_id=causal_event_id,
                    db=connection,
                )
                if row is None:
                    return None
                if surfacing_changed:
                    event = emit_artifact_removal_event(
                        thread_id=thread_id,
                        artifact_id=artifact_id,
                        artifact_type=row["type"],
                        title=row.get("title"),
                        subtype=(
                            "hidden_from_pane"
                            if kwargs.get("hidden")
                            else "shown_in_pane"
                        ),
                        actor_kind="agent",
                        causal_event_id=causal_event_id,
                        hidden=bool(kwargs.get("hidden")),
                        db=connection,
                    )
        return row, surfacing_changed, event
    finally:
        db.close()
