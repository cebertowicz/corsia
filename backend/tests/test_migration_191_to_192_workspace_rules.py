import sqlite3

import pytest

from events.rules import rule_scope
from infra.database_migrations import MIGRATIONS
from infra.delegation_scope import WORKSPACE_SCOPE_ID
from infra.event_rule_schema import EVENT_RULE_SCHEMA_SQL
from infra.migration_170_to_171_worker_profile import _INDEXES, _RULES
from infra.migration_191_to_192_workspace_rules import migrate_191_to_192
from infra.schema import SCHEMA_VERSION


def _route(db, rule_id, scope):
    db.execute(
        'INSERT INTO event_rules (id, created_at, updated_at, scope, connector, event_type, '
        'action, "to", mode, origin, created_by, authority_ref) '
        "VALUES (?, 'now', 'now', ?, 'gmail', 'email.received', 'deliver', "
        "'thread:writer', 'wake', 'agent:writer', 'writer', 'self')", (rule_id, scope))


def _observe(db, rule_id, scope):
    db.execute(
        "INSERT INTO event_rules (id, created_at, updated_at, kind, scope, connector, origin, "
        "created_by, authority_ref, tool, item_type) VALUES (?, 'now', 'now', 'observe', ?, "
        "'gmail', 'agent:writer', 'writer', 'self', 'mail', 'message')", (rule_id, scope))


def test_migration_preserves_rows_indexes_and_triggers_and_accepts_workspace():
    assert SCHEMA_VERSION == 192
    assert MIGRATIONS[191] is migrate_191_to_192
    assert rule_scope(None) == WORKSPACE_SCOPE_ID
    assert rule_scope('abc') == 'project:abc'
    db = sqlite3.connect(':memory:')
    db.execute(_RULES)
    for statement in _INDEXES:
        db.execute(statement)
    db.execute('CREATE TABLE audit (rule_id TEXT)')
    db.execute('CREATE TRIGGER rule_audit AFTER INSERT ON event_rules '
               'BEGIN INSERT INTO audit VALUES (new.id); END')
    _route(db, 'old', 'project:abc')
    _observe(db, 'old-observe', 'project:abc')
    original = db.execute('SELECT * FROM event_rules').fetchall()
    objects = db.execute("SELECT type, name, sql FROM sqlite_master WHERE tbl_name='event_rules' "
                         "AND sql IS NOT NULL AND type IN ('index','trigger') ORDER BY type, name").fetchall()
    migrate_191_to_192(db)
    assert db.execute('SELECT * FROM event_rules').fetchall() == original
    assert db.execute("SELECT type, name, sql FROM sqlite_master WHERE tbl_name='event_rules' "
                      "AND sql IS NOT NULL AND type IN ('index','trigger') ORDER BY type, name").fetchall() == objects
    _route(db, 'workspace-route', WORKSPACE_SCOPE_ID)
    _observe(db, 'workspace-observe', WORKSPACE_SCOPE_ID)
    assert db.execute('SELECT rule_id FROM audit ORDER BY rowid').fetchall() == [
        ('old',), ('old-observe',), ('workspace-route',), ('workspace-observe',)]
    for insert in (_route, _observe):
        with pytest.raises(sqlite3.IntegrityError):
            insert(db, 'invalid', 'unrelated')
    db.close()



def test_fresh_schema_accepts_workspace_and_rejects_invalid_scope():
    db = sqlite3.connect(':memory:')
    db.executescript(EVENT_RULE_SCHEMA_SQL)
    _route(db, 'route', WORKSPACE_SCOPE_ID)
    _observe(db, 'observe', WORKSPACE_SCOPE_ID)
    with pytest.raises(sqlite3.IntegrityError):
        _route(db, 'invalid', 'unrelated')
    db.close()
