"""Schema 151 -> 152: existing rules become route rules."""
from __future__ import annotations

import sqlite3

import pytest

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.event_rule_schema import EVENT_RULE_SCHEMA_SQL
from infra.migration_140_to_141_worker_starts import migrate_140_to_141
from infra.migration_151_to_152_observe_rules import migrate_151_to_152
from events.observe_rules import sync_shipped_rules
from infra.schema import SCHEMA_VERSION
from tests.test_schema_migration_v141 import _V140

_OBJECTS = (
    "SELECT type,name,sql FROM sqlite_master WHERE tbl_name IN ('event_rules','event_route_decisions') "
    "ORDER BY name"
)


def _v148() -> sqlite3.Connection:
    """The 148 shape of both tables, with a route rule and its decision, from the 140 fixture."""
    db = sqlite3.connect(":memory:")
    db.executescript(_V140)
    db.execute("BEGIN")
    migrate_140_to_141(db)
    db.commit()
    return db


def test_existing_rules_become_route_rules():
    db = _v148()
    db.execute("BEGIN")
    migrate_151_to_152(db)
    migrate_151_to_152(db)  # repeatable: the rebuild does not repeat
    assert db.in_transaction
    db.commit()
    fresh = sqlite3.connect(":memory:")
    fresh.executescript(EVENT_RULE_SCHEMA_SQL)

    assert db.execute(_OBJECTS).fetchall() == fresh.execute(_OBJECTS).fetchall()
    assert db.execute("SELECT id, kind, event_type, action, \"to\", tool, item_type FROM event_rules "
                      "ORDER BY id").fetchall() == [
        ("r1", "route", "pr.merged", "deliver", "thread:w", None, None),
        ("r2", "route", "pr.merged", "run_script", "thread:w", None, None)]
    assert db.execute("SELECT COUNT(*) FROM event_route_decisions").fetchone()[0] == 2


def test_storage_ties_each_kind_to_its_own_fields_and_global_scope_to_a_shipped_rule_set():
    db = sqlite3.connect(":memory:")
    db.executescript(EVENT_RULE_SCHEMA_SQL)
    observe = ("INSERT INTO event_rules (id,created_at,updated_at,kind,scope,connector,origin,created_by,"
               "authority_ref,tool,argv_prefix,item_type) VALUES (?, 'a','a','observe',?,'github',?,'w',"
               "'self',?,?,?)")
    db.execute(observe, ("ok", "project:p", "agent:w", "bash", '["gh","pr","create"]', "pull-request"))
    db.execute(observe, ("set", "global", "rule_set:github@1", "bash", '["gh"]', "pull-request"))
    for scope, origin, tool, prefix, item in (
        ("global", "agent:w", "bash", '["gh"]', "pull-request"),   # global is a rule set's alone
        ("project:p", "agent:w", None, '["gh"]', "pull-request"),  # an observe rule names its tool
        ("project:p", "agent:w", "bash", '["gh"]', None),          # and its artifact type
        ("project:p", "agent:w", "bash", None, "pull-request"),    # and a prefix
        ("project:p", "agent:w", "bash", "[]", "pull-request"),    # with a leading argument
    ):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(observe, ("bad", scope, origin, tool, prefix, item))
    with pytest.raises(sqlite3.IntegrityError):  # a route rule keeps its event type, action and recipient
        db.execute("INSERT INTO event_rules (id,created_at,updated_at,scope,connector,origin,created_by,"
                   "authority_ref) VALUES ('route','a','a','project:p','github','agent:w','w','self')")


def test_a_full_v148_install_takes_the_step_and_ends_where_a_fresh_install_starts():
    assert SCHEMA_VERSION == 152 and MIGRATIONS[151] is migrate_151_to_152
    db = sqlite3.connect(":memory:")
    initialize_schema(db)
    for table in ("event_route_decisions", "event_rules"):
        db.execute(f"DROP TABLE {table}")
    db.executescript(_V140.split("CREATE TABLE automation_schedules")[0])
    db.execute("PRAGMA user_version=148")
    db.commit()
    assert migrate_to_current(db, SCHEMA_VERSION).to_version == 152
    fresh = sqlite3.connect(":memory:")
    initialize_schema(fresh)
    sync_shipped_rules(db)
    sync_shipped_rules(fresh)
    schema = "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    assert db.execute(schema).fetchall() == fresh.execute(schema).fetchall()
    assert db.execute("SELECT id FROM event_rules WHERE kind='observe'").fetchall() == [
        ("ruleset_github_pr_create",)]
    assert not migrate_to_current(db, SCHEMA_VERSION).migrated
