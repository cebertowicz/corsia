"""Canonical writer for artifact identity registration and plan-node origin."""

from __future__ import annotations

import json
import uuid
from typing import Any

from infra.database import write_transaction
from registry.identity_bridge import (
    OWNER_REGISTRATION_STATES,
    OriginRebind,
    _upsert_registration,
)
from registry.ownership import latest_released_owner_thread_id
from registry.registration_events import _append_registration_event, _now, resolve
from registry.retention import (
    RETENTION_DURABLE,
    RETENTION_EPHEMERAL,
    RetentionChangeDenied,
    authorize_and_promote_explicit_durable_retention,
)
from registry.registration_origin import (
    RegistrationOrigin,
    origin_from_tool_context,
    resolve_rebind,
)

_RETENTIONS = (RETENTION_EPHEMERAL, RETENTION_DURABLE)


def _owner_registration(db, artifact_id: str):
    placeholders = ",".join("?" for _ in OWNER_REGISTRATION_STATES)
    return db.execute(
        f"""SELECT * FROM artifact_registrations
            WHERE artifact_id=? AND relationship_state IN ({placeholders})
            ORDER BY CASE relationship_state WHEN 'owner-pinned' THEN 0 ELSE 1 END,
                     updated_at DESC, id ASC LIMIT 1""",
        (artifact_id, *OWNER_REGISTRATION_STATES),
    ).fetchone()


def _ownership_is_released(db, artifact_id: str) -> bool:
    return latest_released_owner_thread_id(artifact_id, db=db) is not None


def _append_registered_event(
    db,
    artifact_id: str,
    actor_thread_id: str,
    old_owner_thread_id: str | None,
    relationship_state: str,
    now: str,
    *,
    metadata: dict[str, Any] | None,
    causal_event_id: str | None = None,
) -> str | None:
    return _append_registration_event(
        db,
        artifact_id=artifact_id,
        event_type="registered",
        actor_thread_id=actor_thread_id,
        authority="register",
        reason=None,
        source_path="registry.registration_writer.register_artifact",
        affected_thread_id=actor_thread_id,
        old_owner_thread_id=old_owner_thread_id,
        new_owner_thread_id=(
            actor_thread_id
            if relationship_state == "owner-active"
            else old_owner_thread_id
        ),
        old_relationship_state=(
            "owner-active" if old_owner_thread_id else None
        ),
        new_relationship_state=relationship_state,
        metadata=metadata,
        now=now,
        causal_event_id=causal_event_id,
    )


def _registration_kind(existing) -> str:
    if existing is None:
        return "new"
    if existing["surfacing"] == "settled":
        return "revived"
    return "rebind"


def _upsert_identity(
    db,
    *,
    integration: str,
    artifact_type: str,
    external_id: str,
    title: str | None,
    description: str | None,
    path: str | None,
    retention: str,
    metadata: dict | None,
    now: str,
):
    candidate_id = str(uuid.uuid4())[:12]
    inserted = db.execute(
        """INSERT INTO artifact_identities
           (id,integration,type,external_id,title,description,path,state,
            retention,metadata,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,'active',?,?,?,?)
           ON CONFLICT(integration,type,external_id) DO NOTHING""",
        (
            candidate_id,
            integration,
            artifact_type,
            external_id,
            title,
            description,
            path,
            retention,
            json.dumps(metadata) if metadata else None,
            now,
            now,
        ),
    ).rowcount == 1
    identity = db.execute(
        """SELECT * FROM artifact_identities
           WHERE integration=? AND type=? AND external_id=?""",
        (integration, artifact_type, external_id),
    ).fetchone()
    return identity, inserted


def _upsert_relationship(
    db,
    *,
    identity,
    inserted: bool,
    owner,
    released: bool,
    parent_thread_id: str,
    title: str | None,
    description: str | None,
    path: str | None,
    now: str,
    rebind: OriginRebind,
) -> tuple[str | None, str]:
    artifact_id = identity["id"]
    if (
        not inserted
        and identity["state"] == "active"
        and owner is None
        and released
    ):
        relationship = "subscriber-recent"
        _upsert_registration(
            db,
            artifact_id=artifact_id,
            parent_thread_id=parent_thread_id,
            relationship_state=relationship,
            now=now,
            rebind=rebind,
        )
        return None, relationship

    if not inserted:
        db.execute(
            """UPDATE artifact_identities
               SET state='active',updated_at=?,title=COALESCE(?,title),
                   description=COALESCE(?,description),path=COALESCE(?,path)
               WHERE id=?""",
            (now, title, description, path, artifact_id),
        )

    old_owner = owner["parent_thread_id"] if owner else None
    owner_thread = old_owner or parent_thread_id
    _upsert_registration(
        db,
        artifact_id=artifact_id,
        parent_thread_id=owner_thread,
        relationship_state="owner-active",
        now=now,
        rebind=rebind if owner_thread == parent_thread_id else None,
    )
    relationship = (
        "owner-active" if parent_thread_id == owner_thread else "subscriber-recent"
    )
    if parent_thread_id != owner_thread:
        _upsert_registration(
            db,
            artifact_id=artifact_id,
            parent_thread_id=parent_thread_id,
            relationship_state=relationship,
            now=now,
            rebind=rebind,
        )
    return old_owner, relationship


def _prepare_identity_registration(
    db,
    *,
    integration: str,
    artifact_type: str,
    external_id: str,
    parent_thread_id: str,
    title: str | None,
    description: str | None,
    path: str | None,
    retention: str | None,
    retention_default: bool,
    reject_retention_change: bool,
    metadata: dict | None,
):
    """Create or load an identity and apply an authorized retention request."""
    now = _now()
    insert_retention = retention or (
        RETENTION_EPHEMERAL if retention_default else RETENTION_DURABLE
    )
    identity, inserted = _upsert_identity(
        db,
        integration=integration,
        artifact_type=artifact_type,
        external_id=external_id,
        title=title,
        description=description,
        path=path,
        retention=insert_retention,
        metadata=metadata,
        now=now,
    )
    artifact_id = identity["id"]
    owner = None if inserted else _owner_registration(db, artifact_id)
    released = (
        not inserted and owner is None and _ownership_is_released(db, artifact_id)
    )
    authorize_and_promote_explicit_durable_retention(
        db,
        identity=identity,
        inserted=inserted,
        owner=owner,
        parent_thread_id=parent_thread_id,
        released=released,
        retention=retention,
        reject_retention_change=reject_retention_change,
        now=now,
    )
    return identity, inserted, owner, released, now


def register_artifact(
    *,
    integration: str,
    type: str,
    external_id: str,
    parent_thread_id: str,
    title: str | None = None,
    description: str | None = None,
    path: str | None = None,
    retention: str | None = None,
    retention_default: bool = False,
    reject_retention_change: bool = False,
    metadata: dict | None = None,
    causal_event_id: str | None = None,
    origin: RegistrationOrigin | None = None,
    db=None,
) -> dict[str, Any]:
    """Register an artifact and rebind only the registering thread's row."""
    if retention is not None and retention not in _RETENTIONS:
        raise ValueError(f"invalid retention: {retention!r}")
    with write_transaction(db, scope="register_artifact") as connection:
        identity, inserted, owner, released, now = _prepare_identity_registration(
            connection,
            integration=integration,
            artifact_type=type,
            external_id=external_id,
            parent_thread_id=parent_thread_id,
            title=title,
            description=description,
            path=path,
            retention=retention,
            retention_default=retention_default,
            reject_retention_change=reject_retention_change,
            metadata=metadata,
        )
        artifact_id = identity["id"]
        caller_before = connection.execute(
            """SELECT surfacing FROM artifact_registrations
               WHERE artifact_id=? AND parent_thread_id=?""",
            (artifact_id, parent_thread_id),
        ).fetchone()
        rebind, origin_metadata = resolve_rebind(
            connection, parent_thread_id, origin
        )

        old_owner, relationship = _upsert_relationship(
            connection,
            identity=identity,
            inserted=inserted,
            owner=owner,
            released=released,
            parent_thread_id=parent_thread_id,
            title=title,
            description=description,
            path=path,
            now=now,
            rebind=rebind,
        )
        result = _finish_registration(
            connection,
            integration,
            type,
            external_id,
            artifact_id,
            parent_thread_id,
            old_owner,
            relationship,
            now,
            origin_metadata,
            _registration_kind(caller_before),
            causal_event_id,
            origin,
        )
    if origin is not None and (db is None or not db.in_transaction):
        from registry.origin_telemetry import emit_committed_origin

        emit_committed_origin(db, artifact_id, parent_thread_id)
    return result


def _finish_registration(
    db,
    integration,
    artifact_type,
    external_id,
    artifact_id,
    parent_thread_id,
    old_owner,
    relationship,
    now,
    origin_metadata,
    registration_kind,
    causal_event_id,
    origin,
):
    event_metadata = dict(origin_metadata or {})
    if origin is not None:
        event_metadata.update(
            {"registration": registration_kind, "relationship": relationship}
        )
    _append_registered_event(
        db,
        artifact_id,
        parent_thread_id,
        old_owner,
        relationship,
        now,
        metadata=event_metadata or None,
        causal_event_id=causal_event_id,
    )
    return resolve(integration, artifact_type, external_id, db=db)
