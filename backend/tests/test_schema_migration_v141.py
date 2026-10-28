"""Schema 140 -> 141: rules that start workers, both tables rebuilt in place with every row kept."""
from __future__ import annotations

import sqlite3

import pytest

from infra.event_rule_schema import EVENT_RULE_SCHEMA_SQL
from infra.migration_140_to_141_worker_starts import migrate_140_to_141

_OBJECTS = (
    "SELECT type,name,sql FROM sqlite_master WHERE tbl_name IN ('event_rules','event_route_decisions') "
    "ORDER BY name"
)

# The 140 shape, reduced to the columns and checks the rebuild must carry or widen. The comment
# holds a ';', as the real DDL's comments may.
_V140 = """
    CREATE TABLE event_rules (
        id TEXT PRIMARY KEY NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1, scope TEXT NOT NULL, connector TEXT NOT NULL,
        event_type TEXT NOT NULL, "match" TEXT NOT NULL DEFAULT '[]',
        action TEXT NOT NULL CHECK (action IN ('deliver', 'drop', 'run_script')), "to" TEXT NOT NULL,
        mode TEXT, note TEXT, origin TEXT NOT NULL, created_by TEXT NOT NULL,
        authority_ref TEXT NOT NULL,
        -- run_script: the writer's own schedule; never a worker
        script_id TEXT CHECK ((action = 'run_script') = (script_id IS NOT NULL)));
    CREATE INDEX idx_event_rules_type ON event_rules(connector, event_type, enabled, scope, created_at);
    CREATE INDEX idx_event_rules_scope ON event_rules(scope, created_at);
    CREATE TABLE event_route_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, event_id TEXT NOT NULL,
        connector TEXT NOT NULL, event_type TEXT NOT NULL, watch_event TEXT NOT NULL,
        artifact_integration TEXT, artifact_type TEXT, artifact_external_id TEXT,
        rule_id TEXT NOT NULL, thread_id TEXT,
        outcome TEXT NOT NULL CHECK (outcome IN ('woke','digested','dropped','held','dead_lettered',
            'ended','duplicate','queued','running','ran','failed','skipped')),
        via TEXT NOT NULL, reason TEXT,
        run_input TEXT CHECK (run_input IS NULL OR (outcome = 'queued' AND json_valid(run_input))));
    CREATE INDEX idx_event_route_decisions_rule ON event_route_decisions(rule_id, thread_id, outcome, at);
    CREATE INDEX idx_event_route_decisions_event ON event_route_decisions(event_id);
    CREATE INDEX idx_event_route_decisions_artifact ON event_route_decisions(artifact_external_id, at);
    CREATE INDEX idx_event_route_decisions_at ON event_route_decisions(at);
    CREATE INDEX idx_event_route_decisions_runs ON event_route_decisions(outcome, id);
    CREATE TABLE automation_schedules (id TEXT PRIMARY KEY, kind TEXT NOT NULL, spec TEXT NOT NULL,
        next_run_at TEXT);
    INSERT INTO event_rules (id,created_at,updated_at,scope,connector,event_type,action,"to",mode,note,
        origin,created_by,authority_ref,script_id) VALUES
        ('r1','a','a','project:p','github','pr.merged','deliver','thread:w','wake','follow up','agent:w','w','self',NULL),
        ('r2','a','a','project:p','github','pr.merged','run_script','thread:w',NULL,NULL,'agent:w','w','self','s1');
    INSERT INTO event_route_decisions (at,event_id,connector,event_type,watch_event,rule_id,thread_id,
        outcome,via,run_input) VALUES
        ('a','e1','github','pr.merged','pr.merged','r1','w','woke','rule',NULL),
        ('a','e1','github','pr.merged','pr.merged','r2','w','queued','rule','{"event":{}}');
    INSERT INTO automation_schedules VALUES ('s1','script','rules',NULL), ('s2','script','3600','b'),
        ('c1','cadence','rules','b');
"""

_START = ("INSERT INTO event_rules (id,created_at,updated_at,scope,connector,event_type,action,\"to\","
          "origin,created_by,authority_ref,workflow_id,request_identity_fact) "
          "VALUES (?, 'a','a','project:p','qa','request','start_worker','thread:w','agent:w','w','self',?,?)")


def _v140() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.executescript(_V140)
    return db


def test_the_rebuild_keeps_every_row_matches_a_fresh_install_and_is_repeatable():
    db = _v140()
    db.execute("BEGIN")
    migrate_140_to_141(db)
    migrate_140_to_141(db)
    assert db.in_transaction
    db.commit()
    fresh = sqlite3.connect(":memory:")
    fresh.executescript(EVENT_RULE_SCHEMA_SQL)

    assert db.execute(_OBJECTS).fetchall() == fresh.execute(_OBJECTS).fetchall()
    assert db.execute("SELECT id, action, mode, note, script_id, workflow_id FROM event_rules ORDER BY id"
                      ).fetchall() == [("r1", "deliver", "wake", "follow up", None, None),
                                       ("r2", "run_script", None, None, "s1", None)]
    assert db.execute("SELECT id, outcome, run_input, request_identity, worker_thread_id "
                      "FROM event_route_decisions ORDER BY id").fetchall() == [
        (1, "woke", None, None, None), (2, "queued", '{"event":{}}', None, None)]
    # A rules-only script loses its 'rules' spec; a cadence script and other kinds keep theirs.
    assert db.execute("SELECT id, spec FROM automation_schedules ORDER BY id").fetchall() == [
        ("c1", "rules"), ("s1", ""), ("s2", "3600")]
    # The id sequence carries over, and the widened checks take a worker start.
    db.execute(_START, ("w1", "flow", "request"))
    db.execute("INSERT INTO event_route_decisions (at,event_id,connector,event_type,watch_event,rule_id,"
               "thread_id,outcome,via,run_input,request_identity,worker_thread_id) VALUES ('b','e2','qa',"
               "'request','request','w1','w','started','rule','{}','pr-1@a1','child')")
    assert db.execute("SELECT MAX(id) FROM event_route_decisions").fetchone()[0] == 3


def test_storage_takes_a_start_worker_rule_only_with_its_workflow_and_identity_fact():
    db = sqlite3.connect(":memory:")
    db.executescript(EVENT_RULE_SCHEMA_SQL)
    db.execute(_START, ("ok", "flow", "request"))
    for rule_id, workflow, fact in (("no-flow", None, "request"), ("no-fact", "flow", None)):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(_START, (rule_id, workflow, fact))
    for outcome in ("returned", "held"):  # an input rides only a queued or started row
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO event_route_decisions (at,event_id,connector,event_type,watch_event,"
                       "rule_id,outcome,via,run_input) VALUES ('a','e','qa','request','request','ok',?,"
                       "'rule','{}')", (outcome,))


def test_a_full_v140_install_takes_the_step_and_ends_where_a_fresh_install_starts():
    from infra.database import initialize_schema
    from infra.database_migrations import MIGRATIONS, migrate_to_current
    from infra.schema import SCHEMA_VERSION

    assert SCHEMA_VERSION == 155 and MIGRATIONS[140] is migrate_140_to_141
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    for table in ("event_route_decisions", "event_rules"):
        db.execute(f"DROP TABLE {table}")
    db.executescript(_V140.split("CREATE TABLE automation_schedules")[0])
    db.execute("PRAGMA user_version=140")
    db.commit()
    result = migrate_to_current(db, SCHEMA_VERSION)
    assert (result.migrated, result.from_version, result.to_version) == (True, 140, 155)
    fresh = sqlite3.connect(":memory:")
    initialize_schema(fresh)
    schema = "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    assert db.execute(schema).fetchall() == fresh.execute(schema).fetchall()
    assert not migrate_to_current(db, SCHEMA_VERSION).migrated
