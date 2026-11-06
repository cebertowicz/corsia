"""Schema 179 -> 180: the replies-by-thread index lands on an existing database and serves the reply read."""

from __future__ import annotations

import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS
from infra.migration_179_to_180_comment_replies_index import migrate_179_to_180
from infra.schema import SCHEMA_VERSION

_INDEX = "idx_doc_comments_parent"


def _fresh() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    return db


def _index_sql(db: sqlite3.Connection) -> str | None:
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (_INDEX,)).fetchone()
    return row[0] if row else None


def test_the_step_is_registered_at_179() -> None:
    assert SCHEMA_VERSION == 181
    assert MIGRATIONS[179] is migrate_179_to_180


def test_the_step_adds_the_index_a_fresh_schema_has_and_runs_twice() -> None:
    with closing(_fresh()) as fresh, closing(_fresh()) as db:
        db.execute(f"DROP INDEX {_INDEX}")
        db.execute("PRAGMA user_version=179")
        assert _index_sql(db) is None

        migrate_179_to_180(db)
        migrate_179_to_180(db)

        assert _index_sql(db) == _index_sql(fresh) is not None


def test_the_decision_reply_read_uses_the_index() -> None:
    with closing(_fresh()) as db:
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM doc_comments WHERE parent_id=? LIMIT 1", ("c",),
        ).fetchall()
        assert any(_INDEX in row[-1] for row in plan), plan


def test_the_step_marks_each_decided_resolved_suggestion_as_closed_by_its_decision() -> None:
    with closing(_fresh()) as db:
        db.execute("DROP INDEX idx_doc_comments_parent")
        db.execute("ALTER TABLE doc_comments DROP COLUMN closed_by_decision")
        rows = [  # id, parent, decision, resolved
            ("decided", None, "accepted", 1), ("discussed", None, "rejected", 1),
            ("open", None, "rejected", 0), ("person", None, None, 1), ("reply", "discussed", None, 1),
        ]
        db.executemany(
            "INSERT INTO doc_comments (id, artifact_id, parent_id, author, body, decision, resolved, created_at, updated_at)"
            " VALUES (?, 'a', ?, 'Robin', 'b', ?, ?, 't', 't')", rows,
        )

        migrate_179_to_180(db)
        migrate_179_to_180(db)

        marked = dict(db.execute("SELECT id, closed_by_decision FROM doc_comments").fetchall())
        assert marked == {"decided": 1, "discussed": 1, "open": 0, "person": 0, "reply": 0}


def test_the_step_skips_a_database_without_comments() -> None:
    with closing(sqlite3.connect(":memory:")) as db:
        migrate_179_to_180(db)
        assert _index_sql(db) is None


def test_the_step_skips_a_comments_table_without_replies() -> None:
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE TABLE doc_comments (id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL)")
        migrate_179_to_180(db)
        assert _index_sql(db) is None
