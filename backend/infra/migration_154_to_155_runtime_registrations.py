"""Remove provider registrations created by thread-end ownership rehoming."""

import sqlite3


def migrate_154_to_155(conn: sqlite3.Connection) -> None:
    # Earlier migrations also support partial historical databases with no registry.
    if conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN "
        "('artifact_registrations','artifact_identities','artifact_registration_events')"
    ).fetchone()[0] != 3:
        return
    conn.execute(
        """DELETE FROM artifact_registrations AS r
           WHERE EXISTS (
               SELECT 1 FROM artifact_identities i
               JOIN artifact_registration_events e ON e.artifact_id=i.id
               WHERE i.id=r.artifact_id
                 AND ((i.integration='claude-code' AND i.type='session')
                   OR (i.integration='codex' AND i.type='thread'))
                 AND e.event_type='ownership_rehomed'
                 AND e.source_path='registry.ownership.rehome_on_thread_end'
                 AND e.reason IN ('thread_ended:archived', 'thread_ended:purged',
                                  'thread_ended:deleted', 'thread_ended:agent_disabled')
                 AND e.new_owner_thread_id=r.parent_thread_id
                 AND r.parent_thread_id != COALESCE((
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
                 ))
           )"""
    )
