from __future__ import annotations

from typing import Any

from infra.database import get_db
from registry.identity_bridge import (
    OWNER_REGISTRATION_STATES,
    RELEASED_OWNER_REGISTRATION_STATES,
    public_row_from_normalized,
)


def project_sentinel(project_id: str) -> str:
    """``parent_thread_id`` sentinel for settings-owned rows."""
    return f"project:{project_id}"


def _projection() -> str:
    return """SELECT i.id AS identity_id, i.integration, i.type, i.external_id,
                     i.title, i.description, i.path, i.state, i.retention,
                     i.metadata,
                     i.created_at, i.updated_at,
                     r.parent_thread_id, r.surfacing,
                     r.metadata AS registration_metadata,
                     r.updated_at AS registration_updated_at,
                     r.relationship_state"""


def _released_owner_clause() -> str:
    released_states = ", ".join(
        f"'{state}'" for state in RELEASED_OWNER_REGISTRATION_STATES
    )
    return """i.state='active'
              AND r.relationship_state IN ({released_states})
              AND r.parent_thread_id=(
                  SELECT CASE WHEN e.event_type='ownership_released'
                              THEN e.old_owner_thread_id END
                  FROM artifact_registration_events e
                  WHERE e.artifact_id=i.id
                    AND e.event_type IN ('ownership_released', 'ownership_rehomed')
                  ORDER BY e.created_at DESC, e.rowid DESC LIMIT 1
              )
              AND NOT EXISTS (
                  SELECT 1 FROM artifact_registrations owner
                  WHERE owner.artifact_id=i.id
                    AND owner.relationship_state IN ('owner-pinned', 'owner-active')
              )""".format(released_states=released_states)


def list_for_project(
    project_id: str,
    *,
    type: str | None = None,
    state: str = "active",
    include_ephemeral: bool = False,
    limit: int = 100,
    offset: int = 0,
    db=None,
) -> dict[str, Any]:
    conn = db or get_db()
    try:
        owner_states = ",".join("?" for _ in OWNER_REGISTRATION_STATES)
        where = [
            "(s.project_id=? OR r.parent_thread_id=?)",
            "NOT (i.integration='claude-code' AND i.type='session')",
        ]
        params: list[Any] = [project_id, project_sentinel(project_id)]
        if state == "archived":
            where.append(
                f"((i.state=? AND r.relationship_state IN ({owner_states})) "
                f"OR ({_released_owner_clause()}))"
            )
            params.extend((state, *OWNER_REGISTRATION_STATES))
        else:
            where.extend(("i.state=?", f"r.relationship_state IN ({owner_states})"))
            params.extend((state, *OWNER_REGISTRATION_STATES))
        if type is not None:
            where.append("i.type=?")
            params.append(type)
        if not include_ephemeral:
            where.append("i.retention='durable'")
        source = (
            "FROM artifact_identities i "
            "JOIN artifact_registrations r ON r.artifact_id=i.id "
            "LEFT JOIN threads s ON s.id=r.parent_thread_id "
            f"WHERE {' AND '.join(where)}"
        )
        total = conn.execute(f"SELECT COUNT(*) {source}", params).fetchone()[0]
        projected = (
            ", CASE WHEN i.state='active' THEN 'archived' "
            "ELSE i.state END AS projected_state"
            if state == "archived"
            else ""
        )
        rows = conn.execute(
            f"{_projection()}{projected}, s.title AS parent_thread_title {source} "
            "ORDER BY i.updated_at DESC, i.id ASC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        artifacts = []
        for row in rows:
            item = public_row_from_normalized(row)
            item["parent_thread_title"] = row["parent_thread_title"]
            artifacts.append(item)
        return {
            "artifacts": artifacts,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    finally:
        if not db:
            conn.close()


def list_for_thread(
    parent_thread_id: str, *, include_archived: bool = False, db=None
) -> list[dict]:
    conn = db or get_db()
    try:
        owner_states = ",".join("?" for _ in OWNER_REGISTRATION_STATES)
        released = _released_owner_clause()
        if include_archived:
            relationship_clause = (
                f"((r.relationship_state IN ({owner_states})) OR ({released}))"
            )
            projected = (
                ", CASE WHEN r.relationship_state IN ('owner-pinned', 'owner-active') "
                "THEN i.state ELSE 'archived' END AS projected_state"
            )
        else:
            relationship_clause = f"r.relationship_state IN ({owner_states})"
            projected = ""
        rows = conn.execute(
            f"""{_projection()}{projected}
                FROM artifact_identities i
                JOIN artifact_registrations r ON r.artifact_id=i.id
                WHERE r.parent_thread_id=?
                  AND {relationship_clause}
                  {"" if include_archived else "AND i.state='active'"}
                ORDER BY i.updated_at DESC, i.id ASC""",
            (parent_thread_id, *OWNER_REGISTRATION_STATES),
        ).fetchall()
        return [public_row_from_normalized(row) for row in rows]
    finally:
        if not db:
            conn.close()


# Every record one thread registered for one integration and type.
_THREAD_RECORDS = """FROM artifact_identities i
                JOIN artifact_registrations r ON r.artifact_id=i.id
                WHERE r.parent_thread_id=? AND i.integration=? AND i.type=?"""


def find_for_thread(
    parent_thread_id: str, integration: str, type: str, db=None
) -> dict | None:
    """The thread's current record of one kind: active and still owned, newest first."""
    conn = db or get_db()
    try:
        owner_states = ",".join("?" for _ in OWNER_REGISTRATION_STATES)
        row = conn.execute(
            f"""{_projection()}
                {_THREAD_RECORDS}
                  AND i.state='active'
                  AND r.relationship_state IN ({owner_states})
                ORDER BY i.updated_at DESC, i.id ASC LIMIT 1""",
            (parent_thread_id, integration, type, *OWNER_REGISTRATION_STATES),
        ).fetchone()
        return public_row_from_normalized(row) if row else None
    finally:
        if not db:
            conn.close()
