"""Schema 126 -> 127: an error that only a seed held open is stamped `recovered`."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_126_to_127_seed_recovered_errors import migrate_126_to_127
from infra.schema import SCHEMA_VERSION


def _history(db: sqlite3.Connection, rows: list[tuple[str, dict]]) -> None:
    db.execute("INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES ('t','u','T','x','x')")
    for n, (role, meta) in enumerate(rows, start=1):
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) VALUES (?,?,?,?,?,?,?)",
            (f"m{n}", "t", role, "x", json.dumps(meta), f"2026-10-18T00:00:{n:02d}", n),
        )


def _recovered(db: sqlite3.Connection, mid: str):
    return json.loads(db.execute("SELECT metadata FROM messages WHERE id=?", (mid,)).fetchone()[0]).get("recovered")


# m1 is the error. While a seed counted as a row to read, it held m1 open past the
# turn that followed, so the 102 -> 103 backfill and every turn start left it unstamped.
_SEED_BETWEEN: list[tuple[str, dict]] = [
    ("system", {"type": "llm_error"}),
    ("system", {"cc": True, "type": "seed"}),
    ("assistant", {"llm_tool_calls": [{"id": "c"}]}),
    ("user", {}),
]
_USER_BETWEEN: list[tuple[str, dict]] = [
    ("system", {"type": "llm_error"}),
    ("user", {}),
    ("assistant", {"llm_tool_calls": [{"id": "c"}]}),
]


def test_the_step_is_registered_at_126() -> None:
    assert SCHEMA_VERSION == 127
    assert MIGRATIONS[126] is migrate_126_to_127


def test_startup_stamps_an_error_only_a_seed_held_open(tmp_path) -> None:
    path = tmp_path / "v126.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        _history(db, _SEED_BETWEEN)
        db.execute("PRAGMA user_version=126")
        db.commit()
        assert _recovered(db, "m1") is None
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert _recovered(db, "m1") is True
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_an_error_the_user_was_addressed_after_stays_open() -> None:
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        _history(db, _USER_BETWEEN)

        migrate_126_to_127(db)

        assert _recovered(db, "m1") is None


def test_the_step_only_adds_stamps_and_runs_again_without_change() -> None:
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        _history(db, _SEED_BETWEEN)
        db.execute("UPDATE messages SET metadata=json_set(metadata, '$.recovered', json('true')) WHERE id='m1'")
        before = db.execute("SELECT id, metadata FROM messages ORDER BY seq").fetchall()

        migrate_126_to_127(db)
        migrate_126_to_127(db)

        assert db.execute("SELECT id, metadata FROM messages ORDER BY seq").fetchall() == before
