import json
import sqlite3


def test_coordination_titles_preserve_custom_and_ambiguous_designations():
    from infra.migration_162_to_163_coordination_titles import migrate_162_to_163

    db = sqlite3.connect(':memory:')
    db.executescript('''
        CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, updated_at TEXT);
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, conductor_thread_id TEXT);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO threads VALUES ('ws',' workspace-conductor ','old'), ('alpha','Conductor','old'),
            ('custom','Custom','old'), ('shared','conductor','old'), ('unusual','workspace-conductor','old');
        INSERT INTO projects VALUES ('a','Alpha','alpha'), ('b','Beta','custom'),
            ('c','Gamma','shared'), ('d','Delta','shared'), ('e','Epsilon','unusual');
    ''')
    db.execute('INSERT INTO settings VALUES (?, ?)', ('corsia_agent_thread_id', json.dumps({'session_id': 'ws'})))
    migrate_162_to_163(db)
    migrate_162_to_163(db)
    assert db.execute('SELECT id,title,updated_at FROM threads ORDER BY id').fetchall() == [
        ('alpha', 'Alpha Conductor', 'old'), ('custom', 'Custom', 'old'),
        ('shared', 'conductor', 'old'), ('unusual', 'workspace-conductor', 'old'), ('ws', 'CORSIA', 'old'),
    ]
    assert db.in_transaction


def test_upgrade_disposable_database_through_runner(tmp_path):
    from infra.database import initialize_schema
    from infra.database_migrations import migrate_to_current
    from infra.schema import SCHEMA_VERSION

    with sqlite3.connect(tmp_path / 'corsia.db') as db:
        initialize_schema(db)
        db.execute('PRAGMA user_version=162')
        db.executemany(
            "INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES (?,'u',?,'old','old')",
            [('ws', 'workspace-conductor'), ('alpha', 'Conductor'), ('custom', 'My name')],
        )
        db.execute("INSERT INTO settings (key,value,updated_at) VALUES (?,?,?)", (
            'corsia_agent_thread_id', json.dumps({'session_id': 'ws'}), 'old',
        ))
        db.executemany("INSERT INTO projects (id,name,conductor_thread_id,created_at,updated_at) VALUES (?,?,?,'old','old')", [
            ('a', 'Alpha', 'alpha'), ('b', 'Beta', 'custom'),
        ])
        db.commit()
        assert migrate_to_current(db, SCHEMA_VERSION).to_version == 164
        assert db.execute('SELECT id,title,updated_at FROM threads ORDER BY id').fetchall() == [
            ('alpha', 'Alpha Conductor', 'old'), ('custom', 'My name', 'old'), ('ws', 'CORSIA', 'old'),
        ]
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
