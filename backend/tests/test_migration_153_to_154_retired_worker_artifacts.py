"""Backfill releases written by the old settled-worker hand-over."""

import sqlite3

import pytest

from infra.database_migrations import MIGRATIONS
from infra.migration_153_to_154_retired_worker_artifacts import migrate_153_to_154
from infra.schema import SCHEMA_SQL, SCHEMA_VERSION
from registry import models
from registry.ownership import release_artifact
from registry.registration_events import routing_registrations


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_SQL)
    conn.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('p','p','t','t')")
    for thread, parent in (("grandparent", None), ("parent", "grandparent"), ("worker", "parent")):
        conn.execute(
            """INSERT INTO threads
               (id,user_id,project_id,spawned_by,model,created_at,updated_at)
               VALUES (?,'user','p',?,'test/model','t','t')""", (thread, parent),
        )
    conn.commit()
    yield conn
    conn.close()


def _released(db, external_id, *, source="plans.worker_retirement", path=None):
    artifact = {"integration": "workspace", "type": "file", "external_id": external_id}
    models.register_artifact(**artifact, parent_thread_id="worker", path=path, db=db)
    release_artifact(
        **artifact, actor_thread_id=None, authority="thread_lifecycle",
        reason="assignment settled; obligations pass to the delegating thread",
        source_path=source, triage_thread_id="parent", db=db,
    )
    return artifact


def _migrate(db):
    db.commit()
    db.execute("BEGIN")
    migrate_153_to_154(db)
    db.commit()


@pytest.mark.parametrize("parent_ended,worker_ended,expected", [
    (False, True, "parent"), (True, True, "grandparent"), (False, False, "worker"),
])
def test_restores_and_applies_lifecycle(db, parent_ended, worker_ended, expected):
    artifact = _released(db, "result")
    if parent_ended:
        db.execute("UPDATE threads SET lifecycle_state='archived' WHERE id='parent'")
    if worker_ended:
        db.execute("UPDATE threads SET lifecycle_state='archived' WHERE id='worker'")
    _migrate(db)
    state = routing_registrations(**artifact, db=db)
    assert state["owner"]["parent_thread_id"] == expected
    assert state["triage"] is None
    before = db.execute("SELECT COUNT(*) FROM artifact_registration_events").fetchone()[0]
    _migrate(db)
    assert db.execute("SELECT COUNT(*) FROM artifact_registration_events").fetchone()[0] == before


def test_unrelated_release_is_untouched(db):
    artifact = _released(db, "other", source="other.path")
    db.execute("UPDATE threads SET lifecycle_state='archived' WHERE id='worker'")
    before = db.execute("SELECT COUNT(*) FROM artifact_registration_events").fetchone()[0]
    _migrate(db)
    state = routing_registrations(**artifact, db=db)
    assert state["owner"] is None
    assert state["triage"]["parent_thread_id"] == "parent"
    assert db.execute("SELECT COUNT(*) FROM artifact_registration_events").fetchone()[0] == before


def test_worker_folder_stays_with_worker(db, tmp_path):
    artifact = _released(db, "folder", path=str(tmp_path / ".threads/worker/result.md"))
    db.execute("UPDATE threads SET lifecycle_state='archived' WHERE id='worker'")
    _migrate(db)
    assert routing_registrations(**artifact, db=db)["owner"]["parent_thread_id"] == "worker"


def test_later_release_takes_precedence(db):
    artifact = _released(db, "later")
    from registry.ownership import rehome_artifact

    rehome_artifact(
        **artifact, new_owner_thread_id="worker", actor_thread_id=None,
        authority="thread_lifecycle", reason="reclaim", source_path="test", db=db,
    )
    release_artifact(
        **artifact, actor_thread_id=None, authority="thread_lifecycle", reason="other release",
        source_path="other.path", triage_thread_id="parent", db=db,
    )
    before = db.execute("SELECT COUNT(*) FROM artifact_registration_events").fetchone()[0]
    _migrate(db)
    assert routing_registrations(**artifact, db=db)["owner"] is None
    assert db.execute("SELECT COUNT(*) FROM artifact_registration_events").fetchone()[0] == before


def test_registered():
    assert SCHEMA_VERSION == 156
    assert MIGRATIONS[153] is migrate_153_to_154
