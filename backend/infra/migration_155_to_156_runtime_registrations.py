"""Release provider registrations created by thread-end ownership rehoming."""

import sqlite3
import uuid
from datetime import datetime, timezone


def migrate_155_to_156(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
    if {"id", "spawned_by", "confinement", "confined_read_dirs"} <= columns:
        conn.execute(
            "UPDATE threads SET confinement=NULL, confined_read_dirs=NULL "
            "WHERE (confinement IS NOT NULL OR confined_read_dirs IS NOT NULL) "
            "AND (spawned_by IS NULL OR id IN "
            "(SELECT spawned_by FROM threads WHERE spawned_by IS NOT NULL))"
        )
    # Earlier migrations also support partial historical databases with no registry.
    if conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN "
        "('artifact_registrations','artifact_identities','artifact_registration_events')"
    ).fetchone()[0] != 3:
        return
    # Provider types are frozen here: migrations are snapshots of their release.
    rows = conn.execute(
        """SELECT DISTINCT r.id, i.id, COALESCE((
                   SELECT launch.actor_thread_id FROM artifact_registration_events launch
                   WHERE launch.artifact_id=i.id AND launch.event_type='registered'
                   ORDER BY launch.created_at, launch.rowid LIMIT 1
               ), (
                   SELECT first.old_owner_thread_id FROM artifact_registration_events first
                   WHERE first.artifact_id=i.id AND first.event_type='ownership_rehomed'
                     AND first.source_path='registry.ownership.rehome_on_thread_end'
                     AND first.reason IN ('thread_ended:archived', 'thread_ended:purged',
                                          'thread_ended:deleted', 'thread_ended:agent_disabled')
                   ORDER BY first.created_at, first.rowid LIMIT 1
               )) AS launcher, r.parent_thread_id
           FROM artifact_registrations r
           JOIN artifact_identities i ON i.id=r.artifact_id
           JOIN artifact_registration_events e ON e.artifact_id=i.id
           WHERE ((i.integration='claude-code' AND i.type='session')
               OR (i.integration='codex' AND i.type='thread'))
             AND e.event_type='ownership_rehomed'
             AND e.source_path='registry.ownership.rehome_on_thread_end'
             AND e.reason IN ('thread_ended:archived', 'thread_ended:purged',
                              'thread_ended:deleted', 'thread_ended:agent_disabled')
             AND e.new_owner_thread_id=r.parent_thread_id"""
    ).fetchall()
    repaired = {}
    for registration_id, artifact_id, launcher, registrant in rows:
        if not launcher or registrant == launcher:
            continue
        conn.execute("DELETE FROM artifact_registrations WHERE id=?", (registration_id,))
        repaired[artifact_id] = launcher
    now = datetime.now(timezone.utc).isoformat()
    for artifact_id, launcher in repaired.items():
        conn.execute(
            "UPDATE artifact_registrations SET relationship_state='subscriber-recent', updated_at=? "
            "WHERE artifact_id=? AND relationship_state IN ('owner-pinned','owner-active','unassigned-triage')",
            (now, artifact_id),
        )
        conn.execute(
            """INSERT INTO artifact_registration_events
               (id, artifact_id, event_type, actor_thread_id, authority, reason, source_path,
                causal_event_id, affected_thread_id, old_owner_thread_id, new_owner_thread_id,
                old_relationship_state, new_relationship_state, metadata, created_at)
               VALUES (?,?,'ownership_released',NULL,'migration',
                       'release rehomed provider session', 'infra.migration_155_to_156',
                       NULL,?,?,NULL,'owner-active','subscriber-recent',NULL,?)""",
            (str(uuid.uuid4())[:12], artifact_id, launcher, launcher, now),
        )
    # Re-running finds no rehomed registration rows, so it appends no new events.
