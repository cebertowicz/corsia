"""Schema 156 -> 157: undo what thread-end ownership rehoming did to a parent.

A worker's review session moved to its parent made 154 -> 155 copy the worker's
confinement onto the parent's row, and left the parent resuming the worker's
session. This clears that copied confinement and releases those registrations.
"""

import sqlite3
import uuid
from datetime import datetime, timezone


# A review or damaged Claude Code record the thread holds that no thread-end rehome
# moved to it: its own launch record, which keeps 154 -> 155's fail-closed copy.
_OWN_CLOSED_RECORD = """
    SELECT 1 FROM artifact_registrations r INDEXED BY {index_name} CROSS JOIN artifact_identities i ON r.artifact_id=i.id
    WHERE r.parent_thread_id=threads.id AND i.integration='claude-code' AND i.type='session'
      AND (NOT json_valid(i.metadata)
           OR (json_extract(i.metadata, '$.claude_invocation.confinement') IS NOT NULL
               AND (json_type(i.metadata, '$.claude_invocation.confinement') <> 'text'
                    OR json_extract(i.metadata, '$.claude_invocation.confinement') <> 'none'
                    OR (json_type(i.metadata, '$.claude_invocation.add_dirs') IS NOT NULL
                        AND (json_type(i.metadata, '$.claude_invocation.add_dirs') <> 'array'
                             OR json_array_length(i.metadata, '$.claude_invocation.add_dirs') > 0)))))
      AND NOT EXISTS (
          SELECT 1 FROM artifact_registration_events moved
          WHERE moved.artifact_id=i.id AND moved.event_type='ownership_rehomed'
            AND moved.source_path='registry.ownership.rehome_on_thread_end'
            AND moved.new_owner_thread_id=threads.id)
"""


def migrate_156_to_157(conn: sqlite3.Connection) -> None:
    # Earlier migrations also support partial historical databases with no registry;
    # 154 -> 155 copied no confinement into those.
    if conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN "
        "('artifact_registrations','artifact_identities','artifact_registration_events')"
    ).fetchone()[0] != 3:
        return
    index_name = "migration_157_registrations_thread"
    while conn.execute("SELECT 1 FROM sqlite_master WHERE name=? COLLATE NOCASE", (index_name,)).fetchone():
        index_name += "_"
    conn.execute(f"CREATE INDEX {index_name} ON artifact_registrations(parent_thread_id, artifact_id)")
    try:
        _clear_copied_confinement(conn, index_name)
    finally:
        conn.execute(f"DROP INDEX {index_name}")
    _repair_registrations(conn)


def _clear_copied_confinement(conn: sqlite3.Connection, index_name: str) -> None:
    # A review thread has no CORSIA tools, so a root, a thread that spawned workers, or
    # one a thread-end rehome handed a session (its worker may since be deleted) got its
    # confinement from a moved record, unless it holds a closed one of its own.
    conn.execute(
        "UPDATE threads SET confinement=NULL, confined_read_dirs=NULL "
        "WHERE (confinement IS NOT NULL OR confined_read_dirs IS NOT NULL) "
        "AND (spawned_by IS NULL OR id IN "
        "(SELECT spawned_by FROM threads WHERE spawned_by IS NOT NULL) OR id IN "
        "(SELECT new_owner_thread_id FROM artifact_registration_events "
        "WHERE event_type='ownership_rehomed' "
        "AND source_path='registry.ownership.rehome_on_thread_end')) "
        f"AND NOT EXISTS ({_OWN_CLOSED_RECORD.format(index_name=index_name)})"
    )


def _repair_registrations(conn: sqlite3.Connection) -> None:
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
             AND e.new_owner_thread_id=r.parent_thread_id
             AND NOT EXISTS (
                 SELECT 1 FROM artifact_registration_events later
                 JOIN artifact_registration_events prior ON prior.artifact_id=later.artifact_id
                 WHERE later.artifact_id=i.id
                   AND prior.event_type='ownership_rehomed'
                   AND prior.source_path='registry.ownership.rehome_on_thread_end'
                   AND prior.reason IN ('thread_ended:archived', 'thread_ended:purged',
                                        'thread_ended:deleted', 'thread_ended:agent_disabled')
                   AND (later.created_at > prior.created_at OR
                        (later.created_at = prior.created_at AND later.rowid > prior.rowid))
                   AND COALESCE(later.source_path, '') != 'registry.ownership.rehome_on_thread_end'
                   AND later.event_type IN ('ownership_rehomed', 'ownership_released', 'registered')
                   AND later.old_owner_thread_id IS NOT later.new_owner_thread_id
             )"""
    ).fetchall()
    repaired = {}
    for registration_id, artifact_id, launcher, registrant in rows:
        if not launcher or registrant == launcher:
            continue
        conn.execute("DELETE FROM artifact_registrations WHERE id=?", (registration_id,))
        repaired[artifact_id] = launcher
    now = datetime.now(timezone.utc).isoformat()
    for artifact_id, launcher in repaired.items():
        _release_remaining_owners(conn, artifact_id, launcher, now)
    # Re-running finds no rehomed registration rows, so it appends no new events.


def _release_remaining_owners(conn, artifact_id, launcher, now):
    owners = conn.execute(
        "SELECT id,parent_thread_id,relationship_state FROM artifact_registrations "
        "WHERE artifact_id=? AND relationship_state IN ('owner-pinned','owner-active','unassigned-triage')",
        (artifact_id,),
    ).fetchall()
    for registration_id, owner, state in owners:
        conn.execute(
            "UPDATE artifact_registrations SET relationship_state='subscriber-recent', updated_at=? WHERE id=?",
            (now, registration_id),
        )
    # If the rehomed row was the only owner, the forward release belongs to
    # the launcher. Otherwise record the actual demoted owner rows.
    released = [(owner, state) for _, owner, state in owners] or [(launcher, 'owner-active')]
    for owner, state in released:
        conn.execute(
            """INSERT INTO artifact_registration_events
               (id, artifact_id, event_type, actor_thread_id, authority, reason, source_path,
                causal_event_id, affected_thread_id, old_owner_thread_id, new_owner_thread_id,
                old_relationship_state, new_relationship_state, metadata, created_at)
               VALUES (?,?,'ownership_released',NULL,'migration',
                       'release rehomed provider session', 'infra.migration_156_to_157',
                       NULL,?,?,NULL,?,'subscriber-recent',NULL,?)""",
            (str(uuid.uuid4())[:12], artifact_id, owner, owner, state, now),
        )
