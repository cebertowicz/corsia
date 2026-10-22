"""Schema 138 -> 139: rules that run scripts, rebuilt in place with every row kept."""
from __future__ import annotations

import sqlite3

import pytest

from infra.event_rule_schema import EVENT_RULE_SCHEMA_SQL
from infra.migration_138_to_139_rule_runs import migrate_138_to_139

_OBJECTS = (
    "SELECT type,name,sql FROM sqlite_master WHERE tbl_name IN ('event_rules','event_route_decisions') "
    "ORDER BY name"
)

# The 137/138 shape, reduced to the columns and checks the rebuild must carry or widen.
_V138 = """
    CREATE TABLE event_rules (
        id TEXT PRIMARY KEY NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1, scope TEXT NOT NULL, connector TEXT NOT NULL,
        event_type TEXT NOT NULL, "match" TEXT NOT NULL DEFAULT '[]',
        action TEXT NOT NULL CHECK (action IN ('deliver', 'drop')), "to" TEXT NOT NULL,
        mode TEXT, note TEXT, origin TEXT NOT NULL, created_by TEXT NOT NULL,
        authority_ref TEXT NOT NULL);
    CREATE INDEX idx_event_rules_type ON event_rules(connector, event_type, enabled, scope, created_at);
    CREATE INDEX idx_event_rules_scope ON event_rules(scope, created_at);
    CREATE TABLE event_route_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, event_id TEXT NOT NULL,
        connector TEXT NOT NULL, event_type TEXT NOT NULL, watch_event TEXT NOT NULL,
        artifact_integration TEXT, artifact_type TEXT, artifact_external_id TEXT,
        rule_id TEXT NOT NULL, thread_id TEXT,
        outcome TEXT NOT NULL CHECK (outcome IN ('woke','digested','dropped','held','dead_lettered','ended','duplicate')),
        via TEXT NOT NULL, reason TEXT);
    CREATE INDEX idx_event_route_decisions_rule ON event_route_decisions(rule_id, thread_id, outcome, at);
    CREATE INDEX idx_event_route_decisions_event ON event_route_decisions(event_id);
    CREATE INDEX idx_event_route_decisions_artifact ON event_route_decisions(artifact_external_id, at);
    CREATE INDEX idx_event_route_decisions_at ON event_route_decisions(at);
    INSERT INTO event_rules VALUES ('r1','a','a',1,'project:p','github','pr.merged','[]','deliver',
        'thread:w','wake','start the follow-up','agent:w','w','self');
    INSERT INTO event_route_decisions (at,event_id,connector,event_type,watch_event,rule_id,thread_id,outcome,via)
        VALUES ('a','e1','github','pr.merged','pr.merged','r1','w','woke','rule');
"""

_RUN_RULE = ('INSERT INTO event_rules (id,created_at,updated_at,scope,connector,event_type,action,"to",'
             "origin,created_by,authority_ref,script_id) "
             "VALUES (?, 'a','a','project:p','github','pr.merged','run_script',?,'agent:w','w','self',?)")


def _v138() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.executescript(_V138)
    return db


def test_the_rebuild_keeps_every_row_matches_a_fresh_install_and_is_repeatable():
    db = _v138()
    db.execute("BEGIN")
    migrate_138_to_139(db)
    migrate_138_to_139(db)
    assert db.in_transaction
    db.commit()
    fresh = sqlite3.connect(":memory:")
    fresh.executescript(EVENT_RULE_SCHEMA_SQL)

    assert db.execute(_OBJECTS).fetchall() == fresh.execute(_OBJECTS).fetchall()
    assert db.execute("SELECT id, action, mode, note, script_id FROM event_rules").fetchall() == [
        ("r1", "deliver", "wake", "start the follow-up", None)]
    assert db.execute("SELECT event_id, outcome, run_input FROM event_route_decisions").fetchall() == [
        ("e1", "woke", None)]
    # The id sequence carries over, so a new decision never reuses an old id.
    db.execute("INSERT INTO event_route_decisions (at,event_id,connector,event_type,watch_event,rule_id,"
               "thread_id,outcome,via,run_input) VALUES ('b','e2','github','pr.merged','pr.merged','r1',"
               "'w','queued','rule','{}')")
    assert db.execute("SELECT MAX(id) FROM event_route_decisions").fetchone()[0] == 2


def test_storage_takes_a_run_script_rule_only_with_its_own_script():
    db = sqlite3.connect(":memory:")
    db.executescript(EVENT_RULE_SCHEMA_SQL)
    db.execute(_RUN_RULE, ("ok", "thread:w", "sched-1"))
    for rule_id, to, script in (("no-script", "thread:w", None), ("other", "thread:x", "sched-1")):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(_RUN_RULE, (rule_id, to, script))
    with pytest.raises(sqlite3.IntegrityError):  # a run's input is only ever on a queued row
        db.execute("INSERT INTO event_route_decisions (at,event_id,connector,event_type,watch_event,rule_id,"
                   "outcome,via,run_input) VALUES ('a','e','github','pr.merged','pr.merged','ok','ran',"
                   "'rule','{}')")
