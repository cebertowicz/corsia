"""Batched registry projections for readers that resolve many artifacts at once."""

from __future__ import annotations

from registry.identity_bridge import (
    OWNER_REGISTRATION_STATES,
    RELEASED_OWNER_REGISTRATION_STATES,
    _registration_columns,
    public_row_from_normalized,
)


def normalized_rows_by_ids(db, artifact_ids) -> dict[str, dict]:
    """Batch the default artifact projection without changing its fallback rules."""
    ids = list(dict.fromkeys(artifact_ids))
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    owner_states = ",".join("?" for _ in OWNER_REGISTRATION_STATES)
    rows = db.execute(
        f"""SELECT {_registration_columns()}
              FROM artifact_identities i
              JOIN artifact_registrations r ON r.artifact_id=i.id
             WHERE i.id IN ({marks})
               AND r.relationship_state IN ({owner_states})
             ORDER BY i.id,
                      CASE r.relationship_state
                        WHEN 'owner-pinned' THEN 0 ELSE 1 END,
                      r.updated_at DESC, r.id ASC""",
        (*ids, *OWNER_REGISTRATION_STATES),
    ).fetchall()
    found: dict[str, dict] = {}
    for row in rows:
        artifact_id = row["identity_id"]
        found.setdefault(artifact_id, public_row_from_normalized(row))

    unresolved = [artifact_id for artifact_id in ids if artifact_id not in found]
    if not unresolved:
        return found
    marks = ",".join("?" for _ in unresolved)
    latest = db.execute(
        f"""WITH ranked AS (
                SELECT artifact_id, event_type, old_owner_thread_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY artifact_id ORDER BY created_at DESC, rowid DESC
                       ) AS position
                  FROM artifact_registration_events
                 WHERE artifact_id IN ({marks})
                   AND event_type IN ('ownership_released', 'ownership_rehomed')
            )
            SELECT artifact_id, old_owner_thread_id
              FROM ranked
             WHERE position=1 AND event_type='ownership_released'""",
        unresolved,
    ).fetchall()
    released = [(row["artifact_id"], row["old_owner_thread_id"]) for row in latest]
    if not released:
        return found
    values = ",".join("(?, ?)" for _ in released)
    released_states = ",".join("?" for _ in RELEASED_OWNER_REGISTRATION_STATES)
    rows = db.execute(
        f"""WITH requested(artifact_id, parent_thread_id) AS (VALUES {values})
            SELECT {_registration_columns()}
              FROM requested
              JOIN artifact_identities i ON i.id=requested.artifact_id AND i.state='active'
              JOIN artifact_registrations r ON r.artifact_id=i.id
                 AND r.parent_thread_id=requested.parent_thread_id
             WHERE r.relationship_state IN ({released_states})
             ORDER BY i.id,
                      CASE r.relationship_state
                        WHEN 'owner-pinned' THEN 0
                        WHEN 'owner-active' THEN 1 ELSE 2 END,
                      r.updated_at DESC, r.id ASC""",
        (*[value for request in released for value in request], *RELEASED_OWNER_REGISTRATION_STATES),
    ).fetchall()
    for row in rows:
        artifact_id = row["identity_id"]
        if artifact_id not in found:
            projected = dict(row)
            projected["projected_state"] = "archived"
            found[artifact_id] = public_row_from_normalized(projected)
    return found


def resolve_normalized_owners(
    db, integration: str, identities: list[tuple[str, str]],
) -> dict[tuple[str, str], dict]:
    """Batch active owner projections by ``(type, external_id)``."""
    requested = list(dict.fromkeys(identities))
    if not requested:
        return {}
    values = ",".join("(?, ?)" for _ in requested)
    owner_states = ",".join("?" for _ in OWNER_REGISTRATION_STATES)
    rows = db.execute(
        f"""WITH requested(type, external_id) AS (VALUES {values})
            SELECT requested.type AS requested_type,
                   requested.external_id AS requested_external_id,
                   {_registration_columns()}
              FROM requested
              JOIN artifact_identities i ON i.integration=? AND i.type=requested.type
                 AND i.external_id=requested.external_id AND i.state='active'
              JOIN artifact_registrations r ON r.artifact_id=i.id
             WHERE r.relationship_state IN ({owner_states})
             ORDER BY requested.type, requested.external_id,
                      CASE r.relationship_state
                        WHEN 'owner-pinned' THEN 0 ELSE 1 END,
                      r.updated_at DESC, r.id ASC""",
        (*[value for request in requested for value in request], integration, *OWNER_REGISTRATION_STATES),
    ).fetchall()
    found: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row["requested_type"], row["requested_external_id"])
        found.setdefault(key, public_row_from_normalized(row))
    return found
