"""Registration routing reads and the append-only registration-event log."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from infra.database import get_db
from registry.identity_bridge import (
    OWNER_REGISTRATION_STATES,
    resolve_normalized_owner,
)

_PASSIVE_ACTIVITY_STATES = ("subscriber-recent",)
_TRIAGE_REGISTRATION_STATES = ("unassigned-triage",)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve(integration: str, type: str, external_id: str, db=None) -> dict | None:
    """Resolve an active identity to its current owner projection."""
    _db = db or get_db()
    try:
        identity = _db.execute(
            "SELECT state FROM artifact_identities "
            "WHERE integration=? AND type=? AND external_id=?",
            (integration, type, external_id),
        ).fetchone()
        if identity is None or identity["state"] != "active":
            return None
        return resolve_normalized_owner(_db, integration, type, external_id)
    finally:
        if not db:
            _db.close()


UPLOAD_IDENTITY_TYPES = ("upload", "image")


def resolve_upload(external_id: str, db=None) -> dict | None:
    """Resolve an active upload identity by file id, whether typed upload or image."""
    _db = db or get_db()
    try:
        identity = _db.execute(
            "SELECT integration, type, state FROM artifact_identities "
            "WHERE integration='upload' AND type IN (?, ?) AND external_id=? "
            "AND state='active' ORDER BY type='image' DESC LIMIT 1",
            (*UPLOAD_IDENTITY_TYPES, external_id),
        ).fetchone()
        if identity is None:
            return None
        return resolve_normalized_owner(
            _db, identity["integration"], identity["type"], external_id,
        )
    finally:
        if not db:
            _db.close()


def routing_registrations(
    integration: str, type: str, external_id: str, db=None
) -> dict[str, Any]:
    """Return routing state for one canonical artifact identity."""
    _db = db or get_db()
    try:
        identity = _db.execute(
            "SELECT * FROM artifact_identities "
            "WHERE integration=? AND type=? AND external_id=? AND state='active'",
            (integration, type, external_id),
        ).fetchone()
        if not identity:
            return {
                "identity": None,
                "owner": None,
                "subscribers": [],
                "triage": None,
            }

        states = (
            *OWNER_REGISTRATION_STATES,
            *_PASSIVE_ACTIVITY_STATES,
            *_TRIAGE_REGISTRATION_STATES,
        )
        placeholders = ",".join("?" for _ in states)
        rows = [
            dict(row)
            for row in _db.execute(
                f"""SELECT *
                    FROM artifact_registrations
                    WHERE artifact_id=? AND relationship_state IN ({placeholders})
                    ORDER BY CASE relationship_state
                        WHEN 'owner-pinned' THEN 0
                        WHEN 'owner-active' THEN 1
                        WHEN 'unassigned-triage' THEN 2
                        ELSE 3
                    END,
                    updated_at DESC""",
                (identity["id"], *states),
            ).fetchall()
        ]
        owner = next(
            (
                row for row in rows
                if row["relationship_state"] in OWNER_REGISTRATION_STATES
            ),
            None,
        )
        triage = next(
            (
                row for row in rows
                if row["relationship_state"] in _TRIAGE_REGISTRATION_STATES
            ),
            None,
        )
        subscribers = [
            row for row in rows
            if row["relationship_state"] in _PASSIVE_ACTIVITY_STATES
        ]
        return {
            "identity": dict(identity),
            "owner": owner,
            "subscribers": subscribers,
            "triage": triage,
        }
    finally:
        if not db:
            _db.close()


_EVENT_INSERT = """INSERT OR IGNORE INTO artifact_registration_events
    (id, artifact_id, event_type, actor_thread_id, authority, reason,
     source_path, causal_event_id, affected_thread_id,
     old_owner_thread_id, new_owner_thread_id, old_relationship_state,
     new_relationship_state, metadata, created_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""


_REGISTRATION_EVENT_SEED = "corsia-artifact-registration"


def _event_values(
    *,
    artifact_id: str,
    event_type: str,
    actor_thread_id: str | None,
    authority: str | None,
    reason: str | None,
    source_path: str | None,
    affected_thread_id: str | None,
    old_owner_thread_id: str | None,
    new_owner_thread_id: str | None,
    old_relationship_state: str | None,
    new_relationship_state: str | None,
    metadata: dict[str, Any] | None,
    now: str,
    causal_event_id: str | None = None,
    idempotency_scope: str | None = None,
) -> tuple:
    """Build one append-only event row with a stable causal identity."""
    causal_key = f"{artifact_id}:{event_type}:{causal_event_id}"
    if idempotency_scope:
        causal_key = f"{causal_key}:{idempotency_scope}"
    event_id = (
        str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"{_REGISTRATION_EVENT_SEED}:{causal_key}",
        ))
        if causal_event_id
        else str(uuid.uuid4())[:12]
    )
    return (
        event_id, artifact_id, event_type, actor_thread_id, authority,
        reason, source_path, causal_event_id, affected_thread_id,
        old_owner_thread_id, new_owner_thread_id, old_relationship_state,
        new_relationship_state,
        json.dumps(metadata, sort_keys=True) if metadata else None, now,
    )


def _append_registration_event(_db, **event) -> str | None:
    """Append one append-only ownership/registration event; returns its id."""
    values = _event_values(**event)
    inserted = _db.execute(_EVENT_INSERT, values).rowcount == 1
    return values[0] if inserted else None


def _append_registration_events(
    _db, events: Iterable[dict[str, Any]],
) -> int:
    """Append a batch of idempotent registration events in one database call."""
    rows = [_event_values(**event) for event in events]
    if not rows:
        return 0
    before = _db.total_changes
    _db.executemany(_EVENT_INSERT, rows)
    return _db.total_changes - before
