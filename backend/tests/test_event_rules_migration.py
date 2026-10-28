"""Schema 135 -> 136: the agent-written rule table and its outcome record, nothing seeded."""
from __future__ import annotations

import sqlite3

import pytest

from events.observe_rules import sync_shipped_rules
from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.event_rule_schema import EVENT_RULE_SCHEMA_SQL
from infra.migration_135_to_136_event_rules import migrate_135_to_136
from infra.schema import SCHEMA_VERSION

_TABLES = ("event_rules", "event_route_decisions")
_OBJECTS = (
    "SELECT type,name,sql FROM sqlite_master WHERE tbl_name IN ('event_rules','event_route_decisions') "
    "ORDER BY name"
)


def _fresh() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    initialize_schema(db)
    return db


def _dev_135() -> sqlite3.Connection:
    db = _fresh()
    for table in _TABLES:
        db.execute(f"DROP TABLE {table}")
    db.execute("PRAGMA user_version=135")
    db.commit()
    return db


def test_upgrade_from_135_matches_a_fresh_install_and_seeds_only_the_shipped_rule(tmp_path):
    fresh = _fresh()
    upgraded = _dev_135()
    result = migrate_to_current(upgraded, SCHEMA_VERSION)
    assert (result.migrated, result.from_version, result.to_version) == (True, 135, 161)
    sync_shipped_rules(upgraded)
    sync_shipped_rules(fresh)
    assert [tuple(r) for r in upgraded.execute(_OBJECTS)] == [tuple(r) for r in fresh.execute(_OBJECTS)]
    shipped = {"event_rules": 1}  # the shipped rule set's observe row, never an agent's
    for table in _TABLES:
        for db in (upgraded, fresh):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == shipped.get(table, 0)


def test_the_migration_is_guarded_and_repeatable():
    db = sqlite3.connect(":memory:")
    migrate_135_to_136(db)
    migrate_135_to_136(db)
    plain = sqlite3.connect(":memory:")
    plain.executescript(EVENT_RULE_SCHEMA_SQL)
    assert db.execute(_OBJECTS).fetchall() == plain.execute(_OBJECTS).fetchall()


_INSERT = ('INSERT INTO event_rules (id,created_at,updated_at,scope,connector,event_type,"match",'
           'action,"to",mode,note,origin,created_by,authority_ref) '
           "VALUES ('r','a','a',?,'github','pr.merged',?,?,?,?,?,'agent:w','w','self')")


def test_a_valid_rule_stores():
    db = sqlite3.connect(":memory:")
    db.executescript(EVENT_RULE_SCHEMA_SQL)
    db.execute(_INSERT, ("project:p", '[{"field":"repo","op":"eq","value":"o/r"}]',
                         "deliver", "thread:w", "wake", "start the follow-up"))
    db.execute(_INSERT.replace("'r'", "'d'"), ("project:p", "[]", "drop", "thread:w", None, None))


@pytest.mark.parametrize("scope,match,action,to,mode,note", [
    ("global", "[]", "deliver", "thread:w", "wake", None),
    ("project:p", "{}", "deliver", "thread:w", "wake", None),
    ("project:p", '[{"field":"repo","op":"like","value":"x"}]', "deliver", "thread:w", "wake", None),
    ("project:p", '[{"field":"repo","op":"in","value":"x"}]', "deliver", "thread:w", "wake", None),
    ("project:p", '[{"field":"repo","op":"eq"}]', "deliver", "thread:w", "wake", None),
    ("project:p", "[" + ",".join(['{"field":"repo","op":"present"}'] * 5) + "]",
     "deliver", "thread:w", "wake", None),
    ("project:p", "[]", "deliver", "role:reviewer", "wake", None),
    ("project:p", "[]", "deliver", "thread:w", None, None),
    ("project:p", "[]", "drop", "thread:other", None, None),
    ("project:p", "[]", "drop", "thread:w", "wake", None),
    ("project:p", "[]", "drop", "thread:w", None, "a note on a drop"),
    ("project:p", "[]", "deliver", "thread:w", "wake", "x" * 501),
])
def test_invalid_rules_are_refused_by_storage(scope, match, action, to, mode, note):
    db = sqlite3.connect(":memory:")
    db.executescript(EVENT_RULE_SCHEMA_SQL)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(_INSERT, (scope, match, action, to, mode, note))
