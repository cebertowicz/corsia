"""find_for_thread starts from the thread's own registrations.

With no ANALYZE statistics SQLite drove the lookup from every identity of the
integration and type (one claude-code session per thread ever) and probed
registrations once per row, so each checkpoint read grew with the install's age.
"""

import sqlite3
import uuid

from infra.schema import SCHEMA_SQL
from registry.identity_bridge import OWNER_REGISTRATION_STATES
from registry.listing import _THREAD_RECORDS, _projection, find_for_thread

NOW = "2026-11-14T00:00:00+00:00"


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    return db


def _register(db, integration, type_, thread, external_id, *, updated=NOW, state="owner-active"):
    artifact_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO artifact_identities (id, integration, type, external_id,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (artifact_id, integration, type_, external_id, NOW, updated),
    )
    db.execute(
        "INSERT INTO artifact_registrations (id, artifact_id, parent_thread_id,"
        " relationship_state, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (uuid.uuid4().hex, artifact_id, thread, state, NOW, NOW),
    )


def test_the_lookup_is_driven_by_the_threads_registrations():
    db = _db()
    states = ",".join("?" for _ in OWNER_REGISTRATION_STATES)
    plan = [
        row[3]
        for row in db.execute(
            f"EXPLAIN QUERY PLAN {_projection()} {_THREAD_RECORDS}"
            f" AND i.state='active' AND r.relationship_state IN ({states})"
            " ORDER BY i.updated_at DESC, i.id ASC LIMIT 1",
            ("thread", "claude-code", "session", *OWNER_REGISTRATION_STATES),
        )
    ]
    assert plan[0].startswith("SEARCH r USING INDEX idx_artifact_registrations_thread"), plan
    assert "(id=?)" in plan[1], plan


def test_it_returns_the_threads_newest_owned_record_among_many_threads():
    db = _db()
    for index in range(50):
        _register(db, "claude-code", "session", f"other-{index}", f"session-{index}")
    _register(db, "local", "document", "thread", "doc")
    _register(db, "claude-code", "session", "thread", "older", updated="2026-11-13T00:00:00+00:00")
    _register(db, "claude-code", "session", "thread", "newest")
    _register(db, "claude-code", "session", "thread", "released", updated="2026-11-15T00:00:00+00:00",
              state="subscriber-recent")

    found = find_for_thread("thread", "claude-code", "session", db=db)

    assert found["external_id"] == "newest"
    assert find_for_thread("thread", "codex", "thread", db=db) is None
    assert find_for_thread("nobody", "claude-code", "session", db=db) is None
