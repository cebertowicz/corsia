"""Artifact identity and registration CRUD."""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from infra.database import get_db, write_transaction
from registry.identity_bridge import (
    OWNER_REGISTRATION_STATES as _OWNER_REGISTRATION_STATES,
    PANE_REGISTRATION_STATES as _PANE_REGISTRATION_STATES,
    normalized_row_by_id as _normalized_row_by_id,
    normalized_rows_by_ids as _normalized_rows_by_ids,
)
from registry.registration_events import (
    _append_registration_event,  # noqa: F401 — re-exported for callers
    _now,
    resolve,  # noqa: F401 — re-exported for callers
    routing_registrations,
)
from registry.listing import (
    find_for_thread,
    list_for_project,
    list_for_thread,
    project_sentinel,
)

from registry.ownership import (  # noqa: F401 — re-exported for callers
    latest_released_owner_thread_id,
    rehome_artifact,
    release_artifact,
)
from registry.registration_writer import (  # noqa: F401 — compatibility re-exports
    RETENTION_DURABLE,
    RETENTION_EPHEMERAL,
    RegistrationOrigin,
    RetentionChangeDenied,
    register_artifact,
)


# Registration writes are owned by registry.registration_writer.


def record_artifact_change(
    *,
    artifact_id: str,
    internal_event: str,
    external_id: str | None,
    summary: str,
    provenance: dict[str, Any],
    actor_thread_id: str | None = None,
    db=None,
    chip: str | None = None,
) -> dict[str, Any] | None:
    """Append one passive artifact change regardless of subscriber count.

    ``summary`` is what a subscribing agent reads; ``chip`` is the shorter line
    the transcript shows when the summary reads as machine text.
    """
    with write_transaction(db, scope="record_artifact_change") as _db:
        from infra.passive_changes import append_change

        created_at = _now()
        sequence = append_change(
            _db,
            object_kind="artifact",
            object_id=artifact_id,
            change_kind=internal_event,
            summary=summary,
            details={
                "external_id": external_id,
                "provenance": provenance,
                **({"chip": chip} if chip else {}),
            },
            actor_thread_id=actor_thread_id,
            created_at=created_at,
        )
        row = {"seq": sequence, "artifact_id": artifact_id,
               "internal_event": internal_event, "external_id": external_id,
               "summary": summary, "provenance": provenance,
               "created_at": created_at}
        return row


def get_artifact(artifact_id: str, db=None) -> dict | None:
    """Fetch the public projection for one artifact identity."""
    _db = db or get_db()
    try:
        return _normalized_row_by_id(_db, artifact_id)
    finally:
        if not db:
            _db.close()


def get_artifacts(artifact_ids, db=None) -> dict[str, dict]:
    """Fetch default public projections for many artifact identities at once."""
    _db = db or get_db()
    try:
        return _normalized_rows_by_ids(_db, artifact_ids)
    finally:
        if not db:
            _db.close()


def find_identity_by_external_ids(
    integration: str,
    type: str,
    external_ids: tuple[str, ...],
    *,
    db=None,
) -> dict[str, Any] | None:
    """Return the first raw identity matching the caller's ordered candidates.

    Unlike ``resolve()``, this deliberately includes archived and ownerless
    identities so a physical-path compatibility lookup can preserve the same
    artifact id when its external identity becomes logical.
    """
    _db = db or get_db()
    try:
        for external_id in dict.fromkeys(external_ids):
            row = _db.execute(
                "SELECT * FROM artifact_identities "
                "WHERE integration=? AND type=? AND external_id=?",
                (integration, type, external_id),
            ).fetchone()
            if row is not None:
                return dict(row)
        return None
    finally:
        if not db:
            _db.close()


def update_description(artifact_id: str, description: str, *, db=None) -> dict | None:
    """Update an artifact identity description."""
    with write_transaction(db, scope="update_description") as _db:
        cur = _db.execute(
            "UPDATE artifact_identities SET description=?, updated_at=? WHERE id=?",
            (description, _now(), artifact_id),
        )
        if cur.rowcount == 0:
            return None
        return _normalized_row_by_id(_db, artifact_id)


SURFACING_VISIBLE = "visible"
SURFACING_QUIET = "quiet"
_SURFACINGS = (SURFACING_VISIBLE, SURFACING_QUIET)


def update_surfacing(
    artifact_id: str,
    *,
    surfacing: str,
    parent_thread_id: str,
    actor_thread_id: str | None = None,
    causal_event_id: str | None = None,
    db=None,
) -> dict | None:
    """Set one registration's surfacing (hide/restore from that thread's pane).

    Registration-scoped and reversible; never touches artifact lifecycle.
    Appends a registration audit event when the value actually changes.
    ``causal_event_id`` threads the originating turn/action id through to the
    audit row, matching the identifier propagated to the chat-visible removal
    event (see ``registry.removal_events.emit_artifact_removal_event``).
    """
    if surfacing not in _SURFACINGS:
        raise ValueError(f"invalid surfacing: {surfacing!r}")
    with write_transaction(db, scope="update_surfacing") as _db:
        states = _PANE_REGISTRATION_STATES
        placeholders = ",".join("?" for _ in states)
        reg = _db.execute(
            f"""SELECT id, surfacing, relationship_state
                FROM artifact_registrations
                WHERE artifact_id=? AND parent_thread_id=?
                  AND relationship_state IN ({placeholders})
                LIMIT 1""",
            (artifact_id, parent_thread_id, *states),
        ).fetchone()
        if reg is None:
            return None
        now = _now()
        if reg["surfacing"] != surfacing:
            _db.execute(
                "UPDATE artifact_registrations SET surfacing=?, updated_at=? WHERE id=?",
                (surfacing, now, reg["id"]),
            )
            _append_registration_event(
                _db,
                artifact_id=artifact_id,
                event_type="surfacing_changed",
                actor_thread_id=actor_thread_id,
                authority="pane-visibility",
                reason=None,
                source_path="registry.models.update_surfacing",
                causal_event_id=causal_event_id,
                affected_thread_id=parent_thread_id,
                old_owner_thread_id=None,
                new_owner_thread_id=None,
                old_relationship_state=reg["relationship_state"],
                new_relationship_state=reg["relationship_state"],
                metadata={"old_surfacing": reg["surfacing"], "new_surfacing": surfacing},
                now=now,
            )
        return _normalized_row_by_id(
            _db,
            artifact_id,
            parent_thread_id=parent_thread_id,
            registration_states=_PANE_REGISTRATION_STATES,
        )


def retire(
    artifact_id: str,
    *,
    tombstone: bool = False,
    db=None,
) -> None:
    """Retire an artifact as archived or tombstoned."""
    with write_transaction(db, scope="retire_artifact") as _db:
        row = _db.execute(
            "SELECT metadata FROM artifact_identities WHERE id=?", (artifact_id,),
        ).fetchone()
        metadata = json.loads(row["metadata"] or "{}") if row is not None else {}
        if metadata.get("attention_state") in {"raised", "deferred"}:
            from registry import attention

            attention.retire_attention(artifact_id, db=_db)
        state = "tombstone" if tombstone else "archived"
        now = _now()
        _db.execute(
            "UPDATE artifact_identities SET state=?, updated_at=? WHERE id=?",
            (state, now, artifact_id),
        )
        passive_state = "tombstoned" if tombstone else "subscriber-archived"
        _db.execute(
            """UPDATE artifact_registrations
               SET relationship_state=?, updated_at=?
               WHERE artifact_id=?
                 AND relationship_state NOT IN
                     ('owner-pinned', 'owner-active', 'subscriber-parent-chain')""",
            (passive_state, now, artifact_id),
        )


def retire_owned_temporary(
    parent_thread_id: str,
    *,
    db=None,
) -> list[str]:
    """Archive active ephemeral identities owned by one thread."""
    with write_transaction(db, scope="retire_owned_temporary") as _db:
        placeholders = ",".join("?" for _ in _OWNER_REGISTRATION_STATES)
        rows = _db.execute(
            f"""SELECT DISTINCT i.id
                FROM artifact_identities i
                JOIN artifact_registrations r ON r.artifact_id=i.id
                WHERE i.state='active'
                  AND i.retention='ephemeral'
                  AND r.parent_thread_id=?
                  AND r.relationship_state IN ({placeholders})
                ORDER BY i.id""",
            (parent_thread_id, *_OWNER_REGISTRATION_STATES),
        ).fetchall()
        retired = [row["id"] for row in rows]
        for artifact_id in retired:
            retire(artifact_id, db=_db)
        return retired


def retire_missing_temporary_candidates(candidates: list[dict], *, db) -> list[str]:
    """Revalidate prepared missing-file identities and retire them in one write."""
    retired: set[str] = set()
    placeholders = ",".join("?" for _ in _OWNER_REGISTRATION_STATES)
    with write_transaction(db, scope="retire_missing_temporary"):
        for candidate in candidates:
            artifact_id = candidate["id"]
            if artifact_id in retired:
                continue
            current = db.execute(
                f"""SELECT i.id, i.path, r.parent_thread_id,
                           i.updated_at, i.state, i.retention
                    FROM artifact_identities i
                    JOIN artifact_registrations r ON r.artifact_id=i.id
                    WHERE i.id=? AND r.relationship_state IN ({placeholders})""",
                (artifact_id, *_OWNER_REGISTRATION_STATES),
            ).fetchall()
            if any(dict(row) == candidate for row in current):
                retire(artifact_id, tombstone=True, db=db)
                retired.add(artifact_id)
    return sorted(retired)


def update_metadata(
    integration: str, type: str, external_id: str, patch: dict, db=None,
) -> dict | None:
    """Merge ``patch`` into an artifact's metadata JSON by identity."""
    with write_transaction(db, scope="update_metadata") as _db:
        r = _db.execute(
            "SELECT id, metadata FROM artifact_identities "
            "WHERE integration=? AND type=? AND external_id=?",
            (integration, type, external_id),
        ).fetchone()
        if not r:
            return None
        try:
            meta = json.loads(r["metadata"]) if r["metadata"] else {}
        except (TypeError, ValueError):
            meta = None
        if not isinstance(meta, dict):
            # Repair-on-write: unreadable metadata cannot be merged into, so
            # surface it and rebuild the object from the patch alone.
            logger.warning(
                f"registry update_metadata: replacing unreadable metadata on "
                f"{integration}/{type}/{external_id}"
            )
            meta = {}
        for k, v in patch.items():
            if v is None:
                meta.pop(k, None)
            else:
                meta[k] = v
        _db.execute(
            "UPDATE artifact_identities SET metadata=?, updated_at=? WHERE id=?",
            (json.dumps(meta) if meta else None, _now(), r["id"]),
        )
        return _normalized_row_by_id(_db, r["id"])


# Registration helpers moved with the canonical writer.
