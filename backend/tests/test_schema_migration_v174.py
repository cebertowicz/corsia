"""Schema 174: the held-reply table is created on an existing database and matches a fresh install."""
import sqlite3

import pytest

from infra.database_migrations import migrate_to_current
from infra.held_replies_schema import HELD_REPLIES_SCHEMA_SQL
from infra.schema import SCHEMA_SQL, SCHEMA_VERSION


def _normalized(rows):
    return [(kind, name, " ".join((sql or "").split())) for kind, name, sql in rows]


def _at_173() -> sqlite3.Connection:
    assert HELD_REPLIES_SCHEMA_SQL in SCHEMA_SQL
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA_SQL.replace(HELD_REPLIES_SCHEMA_SQL, ""))
    db.execute("PRAGMA user_version=173")
    return db


def test_a_173_database_gains_the_table_and_matches_a_fresh_install():
    db = _at_173()
    assert db.execute("SELECT 1 FROM sqlite_master WHERE name='held_replies'").fetchone() is None

    result = migrate_to_current(db, SCHEMA_VERSION)

    assert (result.from_version, result.to_version) == (173, SCHEMA_VERSION)
    fresh = sqlite3.connect(":memory:")
    fresh.executescript(HELD_REPLIES_SCHEMA_SQL)
    query = "SELECT type,name,sql FROM sqlite_master WHERE tbl_name='held_replies' ORDER BY name"
    assert _normalized(db.execute(query).fetchall()) == _normalized(fresh.execute(query).fetchall())
    assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_the_table_keeps_one_row_per_key_and_refuses_an_unknown_state():
    db = _at_173()
    migrate_to_current(db, SCHEMA_VERSION)
    row = ("u", "s", "r", "text", "tool", "k", "held", "t")
    insert = (
        "INSERT INTO held_replies (id,user_id,sender_thread_id,recipient_thread_id,text,via,"
        "idempotency_key,state,created_at) VALUES (?,?,?,?,?,?,?,?,?)"
    )
    db.execute(insert, ("a", *row))

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(insert, ("b", *row))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(insert, ("c", "u", "s", "r2", "text", "tool", "k", "expired", "t"))
