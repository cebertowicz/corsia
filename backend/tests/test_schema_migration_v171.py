"""Schema 171: a start_worker rule keeps the workflow profile its worker runs under."""
import sqlite3

import pytest

from infra.database_migrations import migrate_to_current
from infra.event_rule_schema import EVENT_RULE_SCHEMA_SQL
from infra.schema import SCHEMA_SQL, SCHEMA_VERSION

_COLUMN = (
    "        -- start_worker: the workflow profile its worker runs under (none: the workflow's base settings).\n"
    "        worker_profile TEXT CHECK (worker_profile IS NULL OR action = 'start_worker'),\n"
)
_ROW = ("INSERT INTO event_rules (id,created_at,updated_at,scope,connector,event_type,action,\"to\",origin,created_by,"
        "authority_ref,workflow_id,request_identity_fact) VALUES (?, 'a','a','project:p','qa','request',"
        "'start_worker','thread:a','agent:a','a','self','flow','request')")


def _at_170() -> sqlite3.Connection:
    assert _COLUMN in EVENT_RULE_SCHEMA_SQL
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA_SQL.replace(EVENT_RULE_SCHEMA_SQL, EVENT_RULE_SCHEMA_SQL.replace(_COLUMN, "")))
    db.execute("PRAGMA user_version=170")
    return db


def test_170_rules_survive_and_the_table_matches_a_fresh_install():
    db = _at_170()
    db.execute(_ROW, ("kept",))
    db.commit()
    assert "worker_profile" not in [row[1] for row in db.execute("PRAGMA table_info(event_rules)")]
    result = migrate_to_current(db, SCHEMA_VERSION)
    assert (result.from_version, result.to_version) == (170, SCHEMA_VERSION)
    assert db.execute("SELECT id, workflow_id, worker_profile FROM event_rules").fetchall() == [("kept", "flow", None)]
    fresh = sqlite3.connect(":memory:")
    fresh.executescript(EVENT_RULE_SCHEMA_SQL)
    query = "SELECT type,name,sql FROM sqlite_master WHERE tbl_name IN ('event_rules') ORDER BY name"
    assert db.execute(query).fetchall() == fresh.execute(query).fetchall()
    again = migrate_to_current(db, SCHEMA_VERSION)
    assert not again.migrated


def test_only_a_start_worker_rule_may_carry_a_profile():
    db = _at_170()
    migrate_to_current(db, SCHEMA_VERSION)
    db.execute(_ROW, ("start",))
    db.execute("UPDATE event_rules SET worker_profile='main' WHERE id='start'")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO event_rules (id,created_at,updated_at,scope,connector,event_type,action,mode,\"to\","
                   "origin,created_by,authority_ref,worker_profile) VALUES ('d','a','a','project:p','qa','request',"
                   "'deliver','wake','registered_owner','agent:a','a','self','main')")
