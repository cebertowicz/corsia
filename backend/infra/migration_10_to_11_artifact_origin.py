"""Schema 10 -> 11: node origin and one-time artifact-pane clean slate."""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3


def migrate_10_to_11(conn: sqlite3.Connection) -> None:
    """Add registration-grain lifecycle state and settle legacy live pane rows."""
    migrated_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "ALTER TABLE artifact_registrations RENAME TO artifact_registrations_v10"
    )
    conn.execute(
        """CREATE TABLE artifact_registrations (
            id TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL REFERENCES artifact_identities(id),
            parent_thread_id TEXT NOT NULL,
            relationship_state TEXT NOT NULL DEFAULT 'owner-active',
            tmp INTEGER NOT NULL DEFAULT 0,
            surfacing TEXT NOT NULL DEFAULT 'visible'
                CHECK (surfacing IN ('visible', 'quiet', 'settled')),
            origin_plan_node_id TEXT,
            node_retired_at TEXT,
            metadata TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(artifact_id, parent_thread_id)
        )"""
    )
    conn.execute(
        """INSERT INTO artifact_registrations
            (id,artifact_id,parent_thread_id,relationship_state,tmp,surfacing,
             origin_plan_node_id,node_retired_at,metadata,created_at,updated_at)
        SELECT id,artifact_id,parent_thread_id,relationship_state,tmp,surfacing,
               NULL,NULL,metadata,created_at,updated_at
          FROM artifact_registrations_v10"""
    )
    conn.execute("DROP TABLE artifact_registrations_v10")
    conn.execute(
        """CREATE INDEX idx_artifact_registrations_thread
           ON artifact_registrations(parent_thread_id, updated_at DESC)"""
    )
    conn.execute(
        """CREATE INDEX idx_artifact_registrations_artifact
           ON artifact_registrations(artifact_id, relationship_state)"""
    )
    conn.execute(
        """UPDATE artifact_registrations AS r
              SET surfacing='settled', node_retired_at=?, updated_at=?
            WHERE r.surfacing='visible'
              AND r.relationship_state IN ('owner-active','subscriber-recent')
              AND EXISTS (
                  SELECT 1 FROM artifact_identities i
                   WHERE i.id=r.artifact_id
                     AND i.state='active'
                     AND NOT (i.integration='claude-code' AND i.type='session')
                     AND NOT (i.integration='codex' AND i.type='thread')
              )""",
        (migrated_at, migrated_at),
    )
