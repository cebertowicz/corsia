"""Repair only provider registrations made by thread-end rehoming."""

import sqlite3

import pytest

from infra.database_migrations import MIGRATIONS
from infra.migration_156_to_157_rehome_repair import migrate_156_to_157
from infra.migration_154_to_155_thread_confinement import migrate_154_to_155
from infra import thread_confinement
from infra.schema import SCHEMA_SQL, SCHEMA_VERSION
from registry import models
from registry.ownership import rehome_artifact


@pytest.fixture(autouse=True)
def _historical_mode(monkeypatch):
    monkeypatch.setattr(thread_confinement, "CONFINEMENT_UNTRUSTED", "review")


@pytest.mark.parametrize("integration,kind", [("claude-code", "session"), ("codex", "thread")])
def test_removes_only_thread_end_rehomes_and_is_idempotent(integration, kind):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    for thread in ("parent", "worker", "other"):
        db.execute("INSERT INTO threads (id,user_id,created_at,updated_at) VALUES (?,'u','t','t')", (thread,))
    db.execute("UPDATE threads SET spawned_by='parent' WHERE id='worker'")
    db.commit()
    metadata = {"claude_invocation": {"confinement": "review", "add_dirs": ["/src"],
                                       "working_dir": "/worker/confined"}}
    artifact = dict(integration=integration, type=kind, external_id="child-session")
    models.register_artifact(**artifact, parent_thread_id="worker", metadata=metadata, db=db)
    rehome_artifact(**artifact, new_owner_thread_id="parent", actor_thread_id=None,
                    authority="thread_lifecycle", reason="thread_ended:archived",
                    source_path="registry.ownership.rehome_on_thread_end", db=db)
    # A contaminated parent may have resumed and registered the same identity.
    models.register_artifact(**artifact, parent_thread_id="parent", db=db)
    other = dict(integration=integration, type=kind, external_id="other-session")
    models.register_artifact(**other, parent_thread_id="worker", db=db)
    rehome_artifact(**other, new_owner_thread_id="other", actor_thread_id=None,
                    authority="test", reason="manual", source_path="test", db=db)
    migrate_154_to_155(db)
    if integration == "claude-code":
        assert thread_confinement.read_dirs("parent", db) == ("/src",)
    migrate_156_to_157(db)
    rows = db.execute("SELECT i.external_id,r.parent_thread_id FROM artifact_registrations r "
                      "JOIN artifact_identities i ON i.id=r.artifact_id ORDER BY 1,2").fetchall()
    assert [tuple(r) for r in rows] == [("child-session", "worker"),
                                        ("other-session", "other"), ("other-session", "worker")]
    if integration == "claude-code":
        assert thread_confinement.read_dirs("parent", db) is None
        assert thread_confinement.read_dirs("worker", db) == ("/src",)
    event_count = db.execute("SELECT COUNT(*) FROM artifact_registration_events").fetchone()[0]
    migrate_156_to_157(db)
    assert [tuple(r) for r in db.execute("SELECT i.external_id,r.parent_thread_id FROM artifact_registrations r "
                                        "JOIN artifact_identities i ON i.id=r.artifact_id ORDER BY 1,2")] == [tuple(r) for r in rows]
    assert db.execute("SELECT COUNT(*) FROM artifact_registration_events").fetchone()[0] == event_count
    db.close()


@pytest.mark.parametrize("integration,kind", [("claude-code", "session"), ("codex", "thread")])
def test_later_deliberate_transfer_is_untouched(integration, kind):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    for thread, parent in (("parent", None), ("worker", "parent"), ("other", None)):
        db.execute("INSERT INTO threads (id,user_id,spawned_by,lifecycle_state,created_at,updated_at) "
                   "VALUES (?,'u',?,?, 't','t')", (thread, parent, "archived" if thread == "worker" else "active"))
    db.commit()
    artifact = dict(integration=integration, type=kind, external_id="session")
    models.register_artifact(**artifact, parent_thread_id="worker", db=db)
    rehome_artifact(**artifact, new_owner_thread_id="parent", actor_thread_id=None,
                    authority="thread_lifecycle", reason="thread_ended:archived",
                    source_path="registry.ownership.rehome_on_thread_end", db=db)
    rehome_artifact(**artifact, new_owner_thread_id="other", actor_thread_id="parent",
                    authority="test", reason="manual", source_path="test", db=db)
    # Equal timestamps must still preserve event order via rowid.
    db.execute("UPDATE artifact_registration_events SET created_at='same'")
    before = [tuple(r) for r in db.execute("SELECT parent_thread_id,relationship_state FROM artifact_registrations ORDER BY 1")]
    events = db.execute("SELECT COUNT(*) FROM artifact_registration_events").fetchone()[0]
    migrate_156_to_157(db)
    assert [tuple(r) for r in db.execute("SELECT parent_thread_id,relationship_state FROM artifact_registrations ORDER BY 1")] == before
    assert ("other", "owner-active") in before
    assert db.execute("SELECT COUNT(*) FROM artifact_registration_events").fetchone()[0] == events
    migrate_156_to_157(db)
    assert db.execute("SELECT COUNT(*) FROM artifact_registration_events").fetchone()[0] == events
    db.close()


def test_release_event_names_the_owner_row_demoted():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    for thread in ("worker", "parent", "other"):
        db.execute("INSERT INTO threads (id,user_id,created_at,updated_at) VALUES (?,'u','t','t')", (thread,))
    db.commit()
    artifact = dict(integration="claude-code", type="session", external_id="session")
    models.register_artifact(**artifact, parent_thread_id="worker", db=db)
    rehome_artifact(**artifact, new_owner_thread_id="parent", actor_thread_id=None,
                    authority="thread_lifecycle", reason="thread_ended:archived",
                    source_path="registry.ownership.rehome_on_thread_end", db=db)
    models.register_artifact(**artifact, parent_thread_id="other", db=db)
    db.execute("UPDATE artifact_registrations SET relationship_state='owner-pinned' WHERE parent_thread_id='other'")
    migrate_156_to_157(db)
    row = db.execute("SELECT affected_thread_id,old_owner_thread_id,old_relationship_state "
                     "FROM artifact_registration_events WHERE source_path='infra.migration_156_to_157'").fetchone()
    assert tuple(row) == ("other", "other", "owner-pinned")
    assert db.execute("SELECT relationship_state FROM artifact_registrations WHERE parent_thread_id='other'").fetchone()[0] == "subscriber-recent"
    db.close()


@pytest.mark.parametrize("integration,kind", [("claude-code", "session"), ("codex", "thread")])
def test_repaired_session_matches_forward_release(integration, kind):
    from core.thread_lifecycle import archive_settled_worker
    from infra.database import write_transaction
    from registry.registration_events import resolve

    def fixture():
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA_SQL)
        for thread, parent in (("parent", None), ("worker", "parent"), ("new", None)):
            db.execute("INSERT INTO threads (id,user_id,spawned_by,created_at,updated_at) "
                       "VALUES (?,'u',?,'t','t')", (thread, parent))
        db.commit()
        artifact = dict(integration=integration, type=kind, external_id="session")
        models.register_artifact(**artifact, parent_thread_id="worker", db=db, metadata={
            "claude_invocation": {"confinement": "review", "add_dirs": ["/src"],
                                  "working_dir": "/worker/confined"}})
        return db, artifact

    repaired, artifact = fixture()
    forward, _ = fixture()
    try:
        rehome_artifact(**artifact, new_owner_thread_id="parent", actor_thread_id=None,
                        authority="thread_lifecycle", reason="thread_ended:archived",
                        source_path="registry.ownership.rehome_on_thread_end", db=repaired)
        migrate_154_to_155(repaired)
        migrate_154_to_155(forward)
        migrate_156_to_157(repaired)
        with write_transaction(forward, scope="test_archive") as conn:
            assert archive_settled_worker(conn, "worker")
        for db in (repaired, forward):
            assert thread_confinement.read_dirs("parent", db) is None
            if integration == "claude-code":
                assert thread_confinement.read_dirs("worker", db) == ("/src",)
            assert resolve(**artifact, db=db) is None
            artifact_id = db.execute("SELECT id FROM artifact_identities WHERE external_id='session'").fetchone()[0]
            latest = db.execute("SELECT event_type,old_owner_thread_id,new_owner_thread_id,"
                                "new_relationship_state FROM artifact_registration_events "
                                "WHERE artifact_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
                                (artifact_id,)).fetchone()
            assert tuple(latest) == ("ownership_released", "worker", None, "subscriber-recent")
            models.register_artifact(**artifact, parent_thread_id="new", db=db)
            row = db.execute("SELECT relationship_state FROM artifact_registrations "
                             "WHERE artifact_id=? AND parent_thread_id='new'", (artifact_id,)).fetchone()
            assert row[0] == "subscriber-recent"
    finally:
        repaired.close()
        forward.close()


def test_persistent_parent_recovers_own_session_after_review_worker_rehome():
    from subagents.cc_runtime_helpers import _artifact_cc_session_id

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.execute("INSERT INTO threads (id,user_id,agent_enabled,created_at,updated_at) "
               "VALUES ('parent','u',1,'t','t')")
    db.execute("INSERT INTO threads (id,user_id,spawned_by,lifecycle_state,created_at,updated_at) "
               "VALUES ('worker','u','parent','archived','t','t')")
    db.commit()
    models.register_artifact(integration="claude-code", type="session", external_id="parent-session",
                             parent_thread_id="parent", db=db)
    child = dict(integration="claude-code", type="session", external_id="worker-session")
    models.register_artifact(**child, parent_thread_id="worker", db=db, metadata={
        "claude_invocation": {"confinement": "review", "add_dirs": ["/src"]}})
    rehome_artifact(**child, new_owner_thread_id="parent", actor_thread_id=None,
                    authority="thread_lifecycle", reason="thread_ended:archived",
                    source_path="registry.ownership.rehome_on_thread_end", db=db)
    db.execute("UPDATE artifact_identities SET updated_at='9999' WHERE external_id='worker-session'")
    assert _artifact_cc_session_id("parent", db) == "worker-session"
    migrate_154_to_155(db)
    assert thread_confinement.read_dirs("parent", db) == ("/src",)
    migrate_156_to_157(db)
    assert thread_confinement.read_dirs("parent", db) is None
    assert thread_confinement.read_dirs("worker", db) == ("/src",)
    assert _artifact_cc_session_id("parent", db) == "parent-session"
    db.close()


@pytest.mark.parametrize("own_review", [False, True])
def test_backfilled_spawned_parent_is_repaired_unless_its_own_record_is_review(own_review):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    for thread, parent in (("root", None), ("parent", "root"), ("worker", "parent")):
        db.execute("INSERT INTO threads (id,user_id,spawned_by,created_at,updated_at) "
                   "VALUES (?,'u',?,'t','t')", (thread, parent))
    db.commit()
    metadata = {"claude_invocation": {"confinement": "review", "add_dirs": ["/src"],
                                      "working_dir": "/worker/confined"}}
    child = dict(integration="claude-code", type="session", external_id="child")
    models.register_artifact(**child, parent_thread_id="worker", metadata=metadata, db=db)
    if own_review:
        models.register_artifact(integration="claude-code", type="session", external_id="own",
                                 parent_thread_id="parent", metadata=metadata, db=db)
    rehome_artifact(**child, new_owner_thread_id="parent", actor_thread_id=None,
                    authority="thread_lifecycle", reason="thread_ended:archived",
                    source_path="registry.ownership.rehome_on_thread_end", db=db)
    migrate_154_to_155(db)
    assert thread_confinement.read_dirs("parent", db) == ("/src",)
    migrate_156_to_157(db)
    # A thread's own review record keeps it closed, children or not.
    assert thread_confinement.read_dirs("parent", db) == (("/src",) if own_review else None)
    assert thread_confinement.read_dirs("worker", db) == ("/src",)
    db.close()


def test_root_with_moved_review_record_is_cleared():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    for thread, parent in (("root", None), ("worker", "root")):
        db.execute("INSERT INTO threads (id,user_id,spawned_by,created_at,updated_at) "
                   "VALUES (?,'u',?,'t','t')", (thread, parent))
    db.commit()
    child = dict(integration="claude-code", type="session", external_id="child")
    models.register_artifact(**child, parent_thread_id="worker", db=db, metadata={
        "claude_invocation": {"confinement": "review", "add_dirs": ["/src"]}})
    rehome_artifact(**child, new_owner_thread_id="root", actor_thread_id=None,
                    authority="thread_lifecycle", reason="thread_ended:archived",
                    source_path="registry.ownership.rehome_on_thread_end", db=db)
    migrate_154_to_155(db)
    assert thread_confinement.read_dirs("root", db) == ("/src",)
    migrate_156_to_157(db)
    assert thread_confinement.read_dirs("root", db) is None
    db.close()


def test_parent_whose_deleted_worker_left_its_review_record_is_cleared():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    for thread, parent in (("root", None), ("parent", "root"), ("worker", "parent")):
        db.execute("INSERT INTO threads (id,user_id,spawned_by,created_at,updated_at) "
                   "VALUES (?,'u',?,'t','t')", (thread, parent))
    db.commit()
    child = dict(integration="claude-code", type="session", external_id="child")
    models.register_artifact(**child, parent_thread_id="worker", db=db, metadata={
        "claude_invocation": {"confinement": "review", "add_dirs": ["/src"]}})
    rehome_artifact(**child, new_owner_thread_id="parent", actor_thread_id=None,
                    authority="thread_lifecycle", reason="thread_ended:deleted",
                    source_path="registry.ownership.rehome_on_thread_end", db=db)
    db.execute("DELETE FROM threads WHERE id='worker'")
    db.commit()
    migrate_154_to_155(db)
    assert thread_confinement.read_dirs("parent", db) == ("/src",)
    migrate_156_to_157(db)
    # No longer anyone's spawned_by, but the rehome event shows the record was moved.
    assert thread_confinement.read_dirs("parent", db) is None
    db.close()


@pytest.mark.parametrize("damaged_moved", [True, False])
def test_v155_damaged_parent_backfill_clears_only_a_moved_record(damaged_moved):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    for thread, parent in (("root", None), ("parent", "root"), ("worker", "parent")):
        db.execute("INSERT INTO threads (id,user_id,spawned_by,created_at,updated_at) "
                   "VALUES (?,'u',?,'t','t')", (thread, parent))
    db.commit()
    artifact = dict(integration="claude-code", type="session", external_id="child")
    models.register_artifact(**artifact, parent_thread_id="worker", db=db, metadata={
        "claude_invocation": {"confinement": "review", "add_dirs": ["/src"]}})
    rehome_artifact(**artifact, new_owner_thread_id="parent", actor_thread_id=None,
                    authority="thread_lifecycle", reason="thread_ended:archived",
                    source_path="registry.ownership.rehome_on_thread_end", db=db)
    if damaged_moved:
        db.execute("UPDATE artifact_identities SET metadata='bad json' WHERE external_id='child'")
    else:
        models.register_artifact(integration="claude-code", type="session", external_id="bad",
                                 parent_thread_id="parent", db=db)
        db.execute("UPDATE artifact_identities SET metadata='bad json' WHERE external_id='bad'")
    migrate_154_to_155(db)
    with pytest.raises(thread_confinement.ConfinementRecordDamaged):
        thread_confinement.read_dirs("parent", db)
    migrate_156_to_157(db)
    if damaged_moved:
        assert thread_confinement.read_dirs("parent", db) is None
    else:
        # Its own damaged record keeps 154 -> 155's fail-closed copy.
        with pytest.raises(thread_confinement.ConfinementRecordDamaged):
            thread_confinement.read_dirs("parent", db)
    if not damaged_moved:
        assert thread_confinement.read_dirs("worker", db) == ("/src",)
    before = db.total_changes
    migrate_156_to_157(db)
    assert db.total_changes == before
    db.close()


def test_absent_or_partial_registry_is_a_noop():
    with sqlite3.connect(":memory:") as db:
        migrate_156_to_157(db)
        db.execute("CREATE TABLE artifact_identities (id TEXT)")
        migrate_156_to_157(db)
        assert db.execute("SELECT COUNT(*) FROM artifact_identities").fetchone()[0] == 0


def test_migration_registered():
    assert SCHEMA_VERSION == 168
    assert MIGRATIONS[156] is migrate_156_to_157
