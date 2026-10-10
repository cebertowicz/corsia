"""Restore durable workspace registrations incorrectly retired with their plan node."""

from __future__ import annotations

import sqlite3


def migrate_21_to_22(conn: sqlite3.Connection) -> None:
    """Restore only durable workspace rows retired before their exemption existed.

    The retirement event supplies the exact prior surfacing. A pane-visibility
    change after that event is a later user decision and must win. The
    exemption comes from the one shared predicate
    (registry.retirement_predicate) rather than a locally hand-copied
    condition, but is pinned to the single durable-workspace kind this
    migration was written to repair: exemptions added later have their own
    cutover and must not silently widen what a v21 database restores.
    """
    from registry.retirement_predicate import (
        DURABLE_WORKSPACE_EXEMPT,
        not_exempt_clause,
    )

    not_exempt = not_exempt_clause(DURABLE_WORKSPACE_EXEMPT)

    conn.execute(
        f"""
        WITH latest_retirement AS (
            SELECT
                e.rowid AS event_rowid,
                e.artifact_id,
                e.affected_thread_id,
                e.old_relationship_state,
                e.metadata,
                ROW_NUMBER() OVER (
                    PARTITION BY e.artifact_id, e.affected_thread_id
                    ORDER BY e.rowid DESC
                ) AS position
            FROM artifact_registration_events AS e
            WHERE e.event_type = 'origin_node_retired'
        )
        UPDATE artifact_registrations AS r
           SET surfacing = json_extract(e.metadata, '$.old_surfacing'),
               node_retired_at = NULL,
               updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
          FROM artifact_identities AS i, latest_retirement AS e
         WHERE r.artifact_id = i.id
           AND e.artifact_id = r.artifact_id
           AND e.affected_thread_id = r.parent_thread_id
           AND r.node_retired_at IS NOT NULL
           AND e.position = 1
           AND json_type(e.metadata, '$.old_surfacing') = 'text'
           AND NOT ({not_exempt})
           AND NOT EXISTS (
                SELECT 1
                FROM artifact_registration_events AS later
                WHERE later.artifact_id = r.artifact_id
                  AND later.affected_thread_id = r.parent_thread_id
                  AND later.event_type = 'surfacing_changed'
                  AND later.rowid > e.event_rowid
           )
        """
    )
