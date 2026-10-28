"""Compare the optimized historical migrations with their original SQL."""
import sqlite3
from unittest.mock import patch
import uuid
from datetime import datetime, timezone

from infra.schema import SCHEMA_SQL
from infra.migration_154_to_155_thread_confinement import migrate_154_to_155
from infra.migration_156_to_157_rehome_repair import migrate_156_to_157


# Historical SQL copied from c4909246.
_REVIEW_RECORD = """
    FROM artifact_identities i JOIN artifact_registrations r ON r.artifact_id=i.id
    WHERE r.parent_thread_id=threads.id AND i.integration='claude-code'
      AND i.type='session' AND json_valid(i.metadata)
      AND json_extract(i.metadata, '$.claude_invocation.confinement')='review'
"""
_DAMAGED_RECORD = """
    NOT json_valid(i.metadata)
    OR (json_extract(i.metadata, '$.claude_invocation.confinement') IS NOT NULL
        AND (json_type(i.metadata, '$.claude_invocation.confinement') <> 'text'
             OR json_extract(i.metadata, '$.claude_invocation.confinement') NOT IN ('none', 'review')
             OR (json_extract(i.metadata, '$.claude_invocation.confinement')='none'
                 AND json_type(i.metadata, '$.claude_invocation.add_dirs') IS NOT NULL
                 AND (json_type(i.metadata, '$.claude_invocation.add_dirs') <> 'array'
                      OR json_array_length(i.metadata, '$.claude_invocation.add_dirs') > 0))))
"""


def _old_154(conn: sqlite3.Connection) -> None:
    """Add each thread's write-once confinement and read dirs, and carry over the
    confinement of every Claude Code review child made before them."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
    if not columns:
        return
    if "confinement" not in columns:
        conn.execute("ALTER TABLE threads ADD COLUMN confinement TEXT")
    if "confined_read_dirs" not in columns:
        conn.execute("ALTER TABLE threads ADD COLUMN confined_read_dirs TEXT")
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"artifact_identities", "artifact_registrations"} <= tables:
        return
    # A damaged session could have been the review record. Preserve the old
    # fail-closed behavior even when another valid record exists for the thread.
    conn.execute(
        "UPDATE threads SET confinement='review', confined_read_dirs='[]' "
        "WHERE confinement IS NULL AND spawned_by IS NOT NULL AND EXISTS ("
        "SELECT 1 FROM artifact_identities i JOIN artifact_registrations r ON r.artifact_id=i.id "
        "WHERE r.parent_thread_id=threads.id AND i.integration='claude-code' "
        f"AND i.type='session' AND ({_DAMAGED_RECORD}))"
    )
    conn.execute(
        "UPDATE threads SET confinement='review', confined_read_dirs=("
        "SELECT COALESCE(json_extract(i.metadata, '$.claude_invocation.add_dirs'), '[]') "
        f"{_REVIEW_RECORD} ORDER BY i.updated_at DESC, i.id ASC LIMIT 1) "
        f"WHERE confinement IS NULL AND EXISTS (SELECT 1 {_REVIEW_RECORD})"
    )


_OWN_CLOSED_RECORD = """
    SELECT 1 FROM artifact_identities i JOIN artifact_registrations r ON r.artifact_id=i.id
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


def _old_157(conn: sqlite3.Connection) -> None:
    # Earlier migrations also support partial historical databases with no registry;
    # 154 -> 155 copied no confinement into those.
    if conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN "
        "('artifact_registrations','artifact_identities','artifact_registration_events')"
    ).fetchone()[0] != 3:
        return
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
        f"AND NOT EXISTS ({_OWN_CLOSED_RECORD})"
    )
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
        _old_release_remaining_owners(conn, artifact_id, launcher, now)
    # Re-running finds no rehomed registration rows, so it appends no new events.


def _old_release_remaining_owners(conn, artifact_id, launcher, now):
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


def _fixture():
    db = sqlite3.connect(':memory:')
    db.executescript(SCHEMA_SQL)
    db.executemany("INSERT INTO threads (id,user_id,spawned_by,created_at,updated_at) VALUES (?,'u',?,'t','t')",
                   [('root', None), ('parent', 'root'), ('child', 'parent'), ('empty', None), ('broken', 'root')])
    for ident, metadata, updated, owner in [
        ('a', '{"claude_invocation":{"confinement":"review","add_dirs":["/a"]}}', '1', 'parent'),
        ('b', '{"claude_invocation":{"confinement":"review","add_dirs":["/b"]}}', '2', 'parent'),
        ('c', 'not json', '3', 'broken'),
        ('d', '{"claude_invocation":{"confinement":"none","add_dirs":[]}}', '4', 'child'),
    ]:
        db.execute("INSERT INTO artifact_identities (id,integration,type,external_id,metadata,created_at,updated_at) VALUES (?,'claude-code','session',?,?,'t',?)", (ident, ident, metadata, updated))
        db.execute("INSERT INTO artifact_registrations (id,artifact_id,parent_thread_id,created_at,updated_at) VALUES (?,?,?,'t','t')", ('r'+ident, ident, owner))
    db.commit()
    return db


def _copy(db):
    other = sqlite3.connect(':memory:')
    db.backup(other)
    return other


def test_154_equivalent():
    old = _fixture()
    old.execute('CREATE INDEX MIGRATION_155_REGISTRATIONS_THREAD ON artifact_registrations(artifact_id)')
    old.execute('ALTER TABLE threads DROP COLUMN confinement')
    old.execute('ALTER TABLE threads DROP COLUMN confined_read_dirs')
    old.commit()
    new = _copy(old)
    _old_154(old)
    migrate_154_to_155(new)
    assert old.execute('SELECT * FROM threads ORDER BY id').fetchall() == new.execute('SELECT * FROM threads ORDER BY id').fetchall()


def test_157_equivalent():
    old = _fixture()
    old.execute('CREATE INDEX MIGRATION_157_REGISTRATIONS_THREAD ON artifact_registrations(artifact_id)')
    old.execute("UPDATE threads SET confinement='review', confined_read_dirs='[]' WHERE id IN ('root','parent','child','broken')")
    old.execute("INSERT INTO artifact_registration_events (id,artifact_id,event_type,actor_thread_id,created_at) VALUES ('registered-a','a','registered','child','1')")
    old.execute("INSERT INTO artifact_registration_events (id,artifact_id,event_type,reason,source_path,old_owner_thread_id,new_owner_thread_id,created_at) VALUES ('moved-a','a','ownership_rehomed','thread_ended:archived','registry.ownership.rehome_on_thread_end','child','parent','2')")
    old.execute("INSERT INTO artifact_registrations (id,artifact_id,parent_thread_id,relationship_state,created_at,updated_at) VALUES ('other-a','a','child','owner-active','t','t')")
    old.execute("INSERT INTO artifact_registration_events (id,artifact_id,event_type,actor_thread_id,created_at) VALUES ('registered-b','b','registered','child','1')")
    old.execute("INSERT INTO artifact_registration_events (id,artifact_id,event_type,reason,source_path,old_owner_thread_id,new_owner_thread_id,created_at) VALUES ('moved-b','b','ownership_rehomed','thread_ended:purged','registry.ownership.rehome_on_thread_end','child','parent','2')")
    old.execute("INSERT INTO artifact_registration_events (id,artifact_id,event_type,reason,source_path,old_owner_thread_id,new_owner_thread_id,created_at) VALUES ('manual-b','b','ownership_rehomed','manual','test','parent','empty','2')")
    old.commit()
    new = _copy(old)
    # Freeze time and IDs so generated release events can be compared byte for byte.
    fixed = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for fn, db, module in [(_old_157, old, __name__), (migrate_156_to_157, new, 'infra.migration_156_to_157_rehome_repair')]:
        with patch(module + '.datetime') as clock, patch.object(uuid, 'uuid4', return_value='fixed-id'):
            clock.now.return_value = fixed
            fn(db)
    for table in ('threads', 'artifact_identities', 'artifact_registrations', 'artifact_registration_events'):
        assert old.execute(f'SELECT * FROM {table} ORDER BY id').fetchall() == new.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()
