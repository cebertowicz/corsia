from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from registry.retirement_predicate import artifact_ended_clause

OWNER_REGISTRATION_STATES = ("owner-pinned", "owner-active")
PANE_REGISTRATION_STATES = (*OWNER_REGISTRATION_STATES, "subscriber-recent")
RELEASED_OWNER_REGISTRATION_STATES = (
    "subscriber-recent",
    "subscriber-archived",
    "tombstoned",
    "unassigned-triage",
)
PARENT_CHAIN_REGISTRATION_STATE = "subscriber-parent-chain"


@dataclass(frozen=True)
class OriginRebind:
    """Resolved registration origin for the caller's row, including unbound."""

    plan_node_id: str | None


def settled_row_rebind(
    db, artifact_id: str, thread_id: str | None,
) -> OriginRebind | None:
    """Unbound rebind for a thread's retired row, so a deliberate hand-off shows it.

    None when no thread is to be shown the artifact, or its row is absent,
    shown, quiet or pinned: there is nothing to lift, and a live row keeps the
    task it is bound to.
    """
    if thread_id is None:
        return None
    settled = db.execute(
        "SELECT 1 FROM artifact_registrations "
        "WHERE artifact_id=? AND parent_thread_id=? AND surfacing='settled'",
        (artifact_id, thread_id),
    ).fetchone()
    return OriginRebind(None) if settled else None


class ParentChainOwnershipError(PermissionError):
    """A digest-only parent-chain subscriber cannot become an owner."""


def _registration_select() -> str:
    return """SELECT i.id AS identity_id, i.integration, i.type, i.external_id,
                     i.title, i.description, i.path, i.state, i.retention,
                     i.metadata,
                     i.created_at, i.updated_at,
                     r.parent_thread_id, r.surfacing,
                     r.metadata AS registration_metadata,
                     r.updated_at AS registration_updated_at,
                     r.relationship_state
              FROM artifact_identities i
              JOIN artifact_registrations r ON r.artifact_id=i.id"""


def resolve_normalized_owner(
    db, integration: str, type: str, external_id: str
) -> dict | None:
    placeholders = ",".join("?" for _ in OWNER_REGISTRATION_STATES)
    row = db.execute(
        _registration_select()
        + f""" WHERE i.integration=? AND i.type=? AND i.external_id=?
                 AND i.state='active'
                 AND r.relationship_state IN ({placeholders})
              ORDER BY CASE r.relationship_state
                       WHEN 'owner-pinned' THEN 0 ELSE 1 END,
                       r.updated_at DESC, r.id ASC
              LIMIT 1""",
        (integration, type, external_id, *OWNER_REGISTRATION_STATES),
    ).fetchone()
    return public_row_from_normalized(row) if row else None


def normalized_row_by_id(
    db,
    artifact_id: str,
    *,
    parent_thread_id: str | None = None,
    registration_states: tuple[str, ...] = OWNER_REGISTRATION_STATES,
) -> dict | None:
    identity = db.execute(
        "SELECT state FROM artifact_identities WHERE id=?", (artifact_id,)
    ).fetchone()
    if identity is None:
        return None

    states = registration_states
    projected_state = None
    if parent_thread_id is None and registration_states == OWNER_REGISTRATION_STATES:
        if identity["state"] != "active":
            states = OWNER_REGISTRATION_STATES
        else:
            owner = _joined_registration(db, artifact_id, states)
            if owner is not None:
                return public_row_from_normalized(owner)
            released_owner = _latest_released_owner_thread_id(db, artifact_id)
            if released_owner is None:
                return None
            parent_thread_id = released_owner
            states = RELEASED_OWNER_REGISTRATION_STATES
            projected_state = "archived"
    elif identity["state"] != "active":
        return None

    row = _joined_registration(
        db, artifact_id, states, parent_thread_id=parent_thread_id
    )
    if row is None:
        return None
    projected = dict(row)
    if projected_state is not None:
        projected["projected_state"] = projected_state
    return public_row_from_normalized(projected)


def _latest_released_owner_thread_id(db, artifact_id: str) -> str | None:
    latest = db.execute(
        """SELECT event_type, old_owner_thread_id
           FROM artifact_registration_events
           WHERE artifact_id=?
             AND event_type IN ('ownership_released', 'ownership_rehomed')
           ORDER BY created_at DESC, rowid DESC LIMIT 1""",
        (artifact_id,),
    ).fetchone()
    if latest is None or latest["event_type"] != "ownership_released":
        return None
    return latest["old_owner_thread_id"]


def _joined_registration(
    db,
    artifact_id: str,
    states: tuple[str, ...],
    *,
    parent_thread_id: str | None = None,
):
    placeholders = ",".join("?" for _ in states)
    parent_clause = " AND r.parent_thread_id=?" if parent_thread_id else ""
    params = (parent_thread_id,) if parent_thread_id else ()
    return db.execute(
        _registration_select()
        + f""" WHERE i.id=? {parent_clause}
                 AND r.relationship_state IN ({placeholders})
              ORDER BY CASE r.relationship_state
                       WHEN 'owner-pinned' THEN 0
                       WHEN 'owner-active' THEN 1 ELSE 2 END,
                       r.updated_at DESC, r.id ASC
              LIMIT 1""",
        (artifact_id, *params, *states),
    ).fetchone()


def public_row_from_normalized(row) -> dict:
    metadata = _merge_metadata(
        row["metadata"],
        _row_value(row, "registration_metadata"),
    )
    return {
        "id": row["identity_id"],
        "integration": row["integration"],
        "type": row["type"],
        "external_id": row["external_id"],
        "parent_thread_id": row["parent_thread_id"],
        "title": row["title"],
        "description": row["description"],
        "path": row["path"],
        "state": _row_value(row, "projected_state") or row["state"],
        "retention": row["retention"],
        "surfacing": row["surfacing"],
        "relationship_state": row["relationship_state"],
        "metadata": json.dumps(metadata, sort_keys=True) if metadata else None,
        "created_at": row["created_at"],
        "updated_at": max(
            timestamp
            for timestamp in (
                _row_value(row, "projected_updated_at"),
                row["updated_at"],
                row["registration_updated_at"],
            )
            if timestamp is not None
        ),
    }


def _row_value(row, key: str):
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _merge_metadata(
    identity_raw: object,
    registration_raw: object,
) -> dict:
    identity_meta, identity_error = _parse_metadata(identity_raw)
    registration_meta, registration_error = _parse_metadata(registration_raw)
    merged = _deep_merge(identity_meta, registration_meta)
    errors = {}
    if identity_error:
        errors["identity"] = identity_error
    if registration_error:
        errors["registration"] = registration_error
    if errors:
        merged["registry_metadata_error"] = errors
    return merged


def _parse_metadata(raw: object) -> tuple[dict, str | None]:
    if not raw:
        return {}, None
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}, "invalid_json"
    if not isinstance(parsed, dict):
        return {}, "not_object"
    return parsed, None


def _deep_merge(base: dict, patch: dict) -> dict:
    merged = dict(base)
    for key, value in patch.items():
        current = merged.get(key)
        merged[key] = (
            _deep_merge(current, value)
            if isinstance(current, dict) and isinstance(value, dict)
            else value
        )
    return merged


def _upsert_registration(
    db,
    *,
    artifact_id: str,
    parent_thread_id: str,
    relationship_state: str,
    now: str,
    rebind: OriginRebind | None = None,
) -> None:
    """Create or update one registration, rebinding only when explicitly asked.

    A rebind lifts a task-scoped retirement, because a new task now holds the
    row. It never lifts an ended artifact's retirement: re-registering a merged
    pull request does not reopen it. A brand new registration of an
    already-ended artifact — a thread registering a merged pull request for
    the first time — is inserted plain, then immediately handed to the same
    shared retirement writer (``registry.node_retirement``) that node
    settlement and the merge event use, so it gets the one audited event
    every other retirement gets instead of a second, silent writer.
    Registration has no after-commit nudge path today (nothing was ever
    visible to invalidate here), so unlike settlement or a merge this does
    not also emit ``artifact_pane.auto_retired`` telemetry.
    """
    from registry import node_retirement

    should_rebind = int(rebind is not None)
    origin_node_id = rebind.plan_node_id if rebind is not None else None
    still_live = f"NOT {artifact_ended_clause('artifact_registrations.artifact_id')}"
    is_fresh = db.execute(
        "SELECT 1 FROM artifact_registrations WHERE artifact_id=? AND parent_thread_id=?",
        (artifact_id, parent_thread_id),
    ).fetchone() is None
    ended = bool(
        db.execute(
            f"SELECT {artifact_ended_clause('?')}", (artifact_id,)
        ).fetchone()[0]
    )
    new_id = str(uuid.uuid4())[:12]
    db.execute(
        f"""INSERT INTO artifact_registrations
           (id, artifact_id, parent_thread_id, relationship_state,
            origin_plan_node_id, retired_at, surfacing, metadata,
            created_at, updated_at)
           VALUES (?,?,?,?,?,NULL,'visible',NULL,?,?)
           ON CONFLICT(artifact_id, parent_thread_id) DO UPDATE SET
               relationship_state=CASE
                   WHEN relationship_state='owner-pinned' THEN 'owner-pinned'
                   ELSE excluded.relationship_state
               END,
               updated_at=excluded.updated_at,
               origin_plan_node_id=CASE WHEN ? THEN ? ELSE origin_plan_node_id END,
               retired_at=CASE
                   WHEN ? AND {still_live} THEN NULL
                   ELSE retired_at
               END,
               surfacing=CASE
                   WHEN ? AND surfacing='settled' AND {still_live} THEN 'visible'
                   ELSE surfacing
               END""",
        (
            new_id, artifact_id, parent_thread_id,
            relationship_state, origin_node_id, now, now,
            should_rebind, origin_node_id, should_rebind, should_rebind,
        ),
    )
    if is_fresh and ended:
        node_retirement.retire_registration_by_id(
            db,
            registration_id=new_id,
            trigger="fresh_registration_of_ended_artifact",
        )
