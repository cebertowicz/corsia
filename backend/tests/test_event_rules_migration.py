"""The live rule table replaces legacy storage without losing binding rows."""
from __future__ import annotations

import json
import sqlite3

import pytest

from connections import binder
from infra.event_rule_schema import EVENT_RULE_SCHEMA_SQL
from infra.migration_133_to_134_event_rules import migrate_133_to_134


def test_migrate_non_ascii_disabled_and_zero_precedence():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('''CREATE TABLE connection_rules (
        id TEXT PRIMARY KEY, scope TEXT, connector TEXT, fact TEXT, value TEXT,
        destination_thread_id TEXT, precedence INTEGER, enabled INTEGER,
        created_at TEXT, updated_at TEXT)''')
    values = [('live', 'global', 'gmail', 'from', 'Élise@例.example', 'owner', 0, 1, 'a', 'b'),
              ('off', 'global', 'gmail', 'from', '停用', 'other', 7, 0, 'c', 'd')]
    db.executemany('INSERT INTO connection_rules VALUES (?,?,?,?,?,?,?,?,?,?)', values)
    migrate_133_to_134(db)
    assert db.execute("SELECT name FROM sqlite_master WHERE name='connection_rules'").fetchone() is None
    row = db.execute("SELECT * FROM event_rules WHERE id='live'").fetchone()
    assert (row['created_at'], row['updated_at'], row['precedence'], row['on'], row['action']) == (
        'a', 'b', 0, 'first_seen', 'deliver')
    assert json.loads(row['match']) == [{'field': 'from', 'op': 'eq', 'value': 'Élise@例.example'}]
    assert [r.id for r in binder.evaluate({'from': 'Élise@例.example'}, 'gmail', db=db)] == ['live']
    assert binder.evaluate({'from': '停用'}, 'gmail', db=db) == []
    assert len(binder.list_rules('gmail', enabled_only=False, db=db)) == 2
    db.close()


def test_missing_legacy_table_and_fresh_install_share_exact_schema():
    migrated = sqlite3.connect(':memory:')
    fresh = sqlite3.connect(':memory:')
    migrate_133_to_134(migrated)
    fresh.executescript(EVENT_RULE_SCHEMA_SQL)
    query = "SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
    assert migrated.execute(query).fetchall() == fresh.execute(query).fetchall()
    assert migrated.execute('SELECT * FROM event_rules ORDER BY id').fetchall() == fresh.execute(
        'SELECT * FROM event_rules ORDER BY id').fetchall()
    migrated.close()
    fresh.close()


@pytest.mark.parametrize('match,on,action,to,mode', [
    ('{}', 'every', 'deliver', 'registered_owner', 'wake'),
    ('[{"field":"repo","op":"prefix","value":"x"}]', 'every', 'deliver', 'registered_owner', 'wake'),
    ('[{"op":"eq","value":"x"}]', 'every', 'deliver', 'registered_owner', 'wake'),
    ('[{"field":"repo","op":"eq"}]', 'every', 'deliver', 'registered_owner', 'wake'),
    ('[]', 'first_seen', 'deliver', 'thread:owner', 'default'),
    ('[{"field":"repo","op":"eq","value":1}]', 'first_seen', 'deliver', 'thread:owner', 'default'),
    ('[]', 'every', 'drop', 'thread:owner', 'wake'),
    ('[]', 'every', 'deliver', None, 'wake'),
])
def test_invalid_states_are_rejected_by_storage(match, on, action, to, mode):
    db = sqlite3.connect(':memory:')
    db.executescript(EVENT_RULE_SCHEMA_SQL)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute('''INSERT INTO event_rules
            (id,created_at,updated_at,connector,"match","on",action,"to",mode)
            VALUES ('bad','a','b','github',?,?,?,?,?)''', (match, on, action, to, mode))
    db.close()
