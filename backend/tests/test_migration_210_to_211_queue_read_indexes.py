"""Schema 210 -> 211: the queue read's two indexes, added in place with every row kept."""
from __future__ import annotations

import json
import sqlite3

from infra.migration_210_to_211_queue_read_indexes import migrate_210_to_211

_INDEXES = ("idx_plan_nodes_user_open", "idx_artifact_identities_attention_state")


def _present(db) -> set[str]:
    marks = ",".join("?" for _ in _INDEXES)
    return {row[0] for row in db.execute(
        f"SELECT name FROM sqlite_master WHERE type='index' AND name IN ({marks})", _INDEXES)}


def test_a_full_v210_install_takes_the_step_and_ends_where_a_fresh_install_starts():
    from infra.database import initialize_schema
    from infra.database_migrations import MIGRATIONS, migrate_to_current
    from infra.schema import SCHEMA_VERSION

    assert SCHEMA_VERSION == 211 and MIGRATIONS[210] is migrate_210_to_211
    fresh = sqlite3.connect(":memory:")
    initialize_schema(fresh)
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    for index in _INDEXES:
        db.execute(f"DROP INDEX {index}")
    db.executemany(
        "INSERT INTO artifact_identities (id,integration,type,external_id,metadata,created_at,updated_at)"
        " VALUES (?,'workspace','document',?,?,'t','t')",
        [("raised", "raised", json.dumps({"attention_state": "raised"})),
         ("malformed", "malformed", "not json"),
         ("bare", "bare", None)],
    )
    db.execute("PRAGMA user_version=210")
    db.commit()

    result = migrate_to_current(db, SCHEMA_VERSION)

    assert (result.migrated, result.from_version, result.to_version) == (True, 210, 211)
    schema = "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    assert db.execute(schema).fetchall() == fresh.execute(schema).fetchall()
    assert _present(db) == set(_INDEXES)
    assert db.execute("SELECT COUNT(*) FROM artifact_identities").fetchone()[0] == 3
    migrate_210_to_211(db)  # a second run changes nothing
    assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_a_database_without_the_tables_is_left_alone():
    db = sqlite3.connect(":memory:")
    migrate_210_to_211(db)
    assert _present(db) == set()
