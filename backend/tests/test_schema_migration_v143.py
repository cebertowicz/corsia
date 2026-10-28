"""Schema 142 -> 143: the decision source-task index, added in place with every row kept."""
from __future__ import annotations

import json
import sqlite3

from infra.migration_142_to_143_decision_source_index import migrate_142_to_143

_INDEX = "idx_artifact_identities_decision_source"


def _index_sql(db):
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (_INDEX,)).fetchone()
    return None if row is None else row[0]


def test_a_full_v142_install_takes_the_step_and_ends_where_a_fresh_install_starts():
    from infra.database import initialize_schema
    from infra.database_migrations import MIGRATIONS, migrate_to_current
    from infra.schema import SCHEMA_VERSION

    assert SCHEMA_VERSION == 155 and MIGRATIONS[142] is migrate_142_to_143
    fresh = sqlite3.connect(":memory:")
    initialize_schema(fresh)
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    db.execute(f"DROP INDEX {_INDEX}")
    rows = [
        ("d1", "plan-decision", json.dumps({"plan_decision": {"source_node_id": "n1", "node_id": "r1"}})),
        ("d2", "plan-decision", "not json"),
        ("u1", "upload", None),
    ]
    db.executemany(
        "INSERT INTO artifact_identities (id,integration,type,external_id,metadata,created_at,updated_at)"
        " VALUES (?,'local',?,?,?,'t','t')",
        [(row_id, kind, row_id, metadata) for row_id, kind, metadata in rows],
    )
    db.execute("PRAGMA user_version=142")
    db.commit()

    result = migrate_to_current(db, SCHEMA_VERSION)

    assert (result.migrated, result.from_version, result.to_version) == (True, 142, 155)
    schema = "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    assert db.execute(schema).fetchall() == fresh.execute(schema).fetchall()
    assert _index_sql(db) is not None
    assert db.execute("SELECT COUNT(*) FROM artifact_identities").fetchone()[0] == 3
    migrate_142_to_143(db)  # a second run changes nothing
    assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_a_task_finds_its_decisions_through_the_index():
    from infra.database import initialize_schema
    from plans import decision_projection

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    queries: list[str] = []
    db.set_trace_callback(
        lambda sql: queries.append(sql)
        if "$.plan_decision.source_node_id" in sql and "FROM artifact_identities i" in sql else None
    )
    assert decision_projection.review_steps(["a", "b"], db=db) == {}
    db.set_trace_callback(None)
    query = queries[0][queries[0].index("SELECT"):]
    used = " ".join(str(row[-1]) for row in db.execute("EXPLAIN QUERY PLAN " + query))

    assert f"INDEXED BY {_INDEX}" in query
    assert f"USING INDEX {_INDEX}" in used
