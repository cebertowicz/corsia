"""The startup migration removes stored quotes while preserving plan intent."""

import sqlite3
from contextlib import closing

from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.migration_137_to_138_plan_intent import migrate_137_to_138
from infra.migration_175_to_176_drop_plan_quotes import migrate_175_to_176
from infra.schema import SCHEMA_VERSION
from tests.plan_writer_authorizer import raw_plan_seed


def test_migrate_v175_preserves_plan_intent_and_removes_quotes():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        migrate_137_to_138(db)
        db.execute("INSERT INTO plans (id, owner_kind, owner_id, created_at, updated_at) VALUES ('p', 'thread', 'owner', 't', 't')")
        with raw_plan_seed(db):
            db.execute("INSERT INTO plan_nodes (id, plan_id, order_key, kind, title, intent, acceptance_criteria, commitment_id, created_at, updated_at) VALUES ('n', 'p', 0, 'task', 'Task', 'Goal', 'Done', 'n', 't', 't')")
        db.execute("INSERT INTO plan_node_intent_words VALUES ('n', 'person', 'message', 'm', 'Old quote', 'origin', 1, 't')")
        db.execute("PRAGMA user_version=175")
        db.commit()

        assert SCHEMA_VERSION == 187 and MIGRATIONS[175] is migrate_175_to_176
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (True, 175, 187)
        assert db.execute("SELECT intent, acceptance_criteria FROM plan_nodes WHERE id='n'").fetchone() == ('Goal', 'Done')
        assert db.execute("SELECT name FROM sqlite_master WHERE name='plan_node_intent_words'").fetchone() is None
        assert db.execute("PRAGMA integrity_check").fetchone() == ('ok',)
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
