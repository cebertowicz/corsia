"""Schema 138 -> 139: rules that run scripts, rebuilt in place with every row kept."""
from __future__ import annotations

import sqlite3

import pytest

from infra.continuation_schema import AUTOMATION_SCHEDULES_SQL
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


_SCHEDULES = "SELECT type,name,sql FROM sqlite_master WHERE tbl_name='automation_schedules' ORDER BY name"
_NULLABLE_NEXT_RUN = "next_run_at TEXT CHECK (next_run_at IS NOT NULL OR kind = 'script')"


def test_a_script_schedule_may_have_no_next_run_after_the_rebuild_and_every_row_is_kept():
    assert _NULLABLE_NEXT_RUN in AUTOMATION_SCHEDULES_SQL
    db = _v138()
    db.executescript(AUTOMATION_SCHEDULES_SQL.replace(_NULLABLE_NEXT_RUN, "next_run_at TEXT NOT NULL"))
    db.execute("INSERT INTO automation_schedules (id,thread_id,scope_id,kind,spec,next_run_at,max_runs,"
               "created_by_actor,created_at,updated_at,script_path) "
               "VALUES ('s1','w','p','script','3600','2026-01-01',5,'w','a','a','/x.sh')")
    db.commit()
    db.execute("BEGIN")
    migrate_138_to_139(db)
    migrate_138_to_139(db)
    db.commit()
    fresh = sqlite3.connect(":memory:")
    fresh.executescript(AUTOMATION_SCHEDULES_SQL)

    assert db.execute(_SCHEDULES).fetchall() == fresh.execute(_SCHEDULES).fetchall()
    assert db.execute("SELECT id, spec, next_run_at, max_runs, script_path FROM automation_schedules"
                      ).fetchall() == [("s1", "3600", "2026-01-01", 5, "/x.sh")]
    db.execute("INSERT INTO automation_schedules (id,thread_id,scope_id,kind,spec,next_run_at,max_runs,"
               "created_by_actor,created_at,updated_at) VALUES ('s2','w','p','script','rules',NULL,1,'w','a','a')")
    with pytest.raises(sqlite3.IntegrityError):  # a clock or cadence still needs its next run
        db.execute("INSERT INTO automation_schedules (id,thread_id,scope_id,kind,spec,next_run_at,max_runs,"
                   "created_by_actor,created_at,updated_at) VALUES ('c','w','p','cadence','60',NULL,1,'w','a','a')")


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


def _v137() -> sqlite3.Connection:
    """A full install as of schema 137: before the plan-intent step (138) and this one (139)."""
    from infra.database import initialize_schema

    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    db.execute("DROP INDEX idx_plan_nodes_intent_node")
    db.execute("ALTER TABLE plan_nodes DROP COLUMN intent_node_id")
    db.execute("ALTER TABLE plan_nodes DROP COLUMN intent")
    db.execute("DROP TABLE plan_node_intent_words")
    for table in ("event_route_decisions", "event_rules", "automation_schedules"):
        db.execute(f"DROP TABLE {table}")
    db.executescript(_V138)
    db.executescript(AUTOMATION_SCHEDULES_SQL.replace(_NULLABLE_NEXT_RUN, "next_run_at TEXT NOT NULL"))
    db.execute("PRAGMA user_version=137")
    db.commit()
    return db


def test_a_v137_install_takes_both_steps_and_ends_where_a_fresh_install_starts():
    from infra.database import initialize_schema
    from infra.database_migrations import MIGRATIONS, migrate_to_current
    from infra.migration_137_to_138_plan_intent import migrate_137_to_138
    from infra.schema import SCHEMA_VERSION

    assert SCHEMA_VERSION == 140
    assert (MIGRATIONS[137], MIGRATIONS[138]) == (migrate_137_to_138, migrate_138_to_139)
    db = _v137()
    result = migrate_to_current(db, SCHEMA_VERSION)
    assert (result.migrated, result.from_version, result.to_version) == (True, 137, 140)
    fresh = sqlite3.connect(":memory:")
    initialize_schema(fresh)
    # Step 138 adds its columns and table with ALTER/CREATE text laid out unlike the fresh DDL, so its two
    # tables are compared by columns, in any order; every other object, the three tables this step
    # rebuilds included, by its SQL.
    altered = ("plan_nodes", "plan_node_intent_words")
    schema = ("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
              f"AND NOT (type='table' AND name IN {altered}) ORDER BY type,name")

    def objects(conn):
        return [(*row[:3], " ".join((row[3] or "").split())) for row in conn.execute(schema)]

    assert objects(db) == objects(fresh)
    for table in altered:
        info = f"SELECT name, type, \"notnull\", dflt_value, pk FROM pragma_table_info('{table}') ORDER BY name"
        assert db.execute(info).fetchall() == fresh.execute(info).fetchall()
    assert db.execute("SELECT id, script_id FROM event_rules").fetchall() == [("r1", None)]
    assert not migrate_to_current(db, SCHEMA_VERSION).migrated
