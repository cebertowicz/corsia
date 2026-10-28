"""Repair only provider registrations made by thread-end rehoming."""

import sqlite3

import pytest

from infra.database_migrations import MIGRATIONS
from infra.migration_154_to_155_runtime_registrations import migrate_154_to_155
from infra.schema import SCHEMA_SQL, SCHEMA_VERSION
from registry import models
from registry.cc_session import registered_confinement
from registry.ownership import rehome_artifact
from subagents.cc_invocation import UNCONFINED


@pytest.mark.parametrize("integration,kind", [("claude-code", "session"), ("codex", "thread")])
def test_removes_only_thread_end_rehomes_and_is_idempotent(integration, kind):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    for thread in ("parent", "worker", "other"):
        db.execute("INSERT INTO threads (id,user_id,created_at,updated_at) VALUES (?,'u','t','t')", (thread,))
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
    if integration == "claude-code":
        assert registered_confinement("parent", db).mode == "review"
    migrate_154_to_155(db)
    rows = db.execute("SELECT i.external_id,r.parent_thread_id FROM artifact_registrations r "
                      "JOIN artifact_identities i ON i.id=r.artifact_id ORDER BY 1,2").fetchall()
    assert [tuple(r) for r in rows] == [("child-session", "worker"),
                                        ("other-session", "other"), ("other-session", "worker")]
    if integration == "claude-code":
        assert registered_confinement("parent", db) == UNCONFINED
        assert registered_confinement("worker", db).mode == "review"
    migrate_154_to_155(db)
    assert [tuple(r) for r in db.execute("SELECT i.external_id,r.parent_thread_id FROM artifact_registrations r "
                                        "JOIN artifact_identities i ON i.id=r.artifact_id ORDER BY 1,2")] == [tuple(r) for r in rows]
    db.close()


def test_absent_or_partial_registry_is_a_noop():
    with sqlite3.connect(":memory:") as db:
        migrate_154_to_155(db)
        db.execute("CREATE TABLE artifact_identities (id TEXT)")
        migrate_154_to_155(db)
        assert db.execute("SELECT COUNT(*) FROM artifact_identities").fetchone()[0] == 0


def test_migration_registered():
    assert SCHEMA_VERSION == 155
    assert MIGRATIONS[154] is migrate_154_to_155
