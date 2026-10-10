"""Schema 92 stores a nullable deferred account-move receipt."""
from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from infra.database_migrations import MIGRATIONS
from infra.migration_91_to_92_deferred_move_receipt import migrate_91_to_92
from infra.schema import SCHEMA_VERSION


def test_v92_adds_nullable_json_deferred_move_idempotently():
    assert SCHEMA_VERSION == 103
    assert MIGRATIONS[91] is migrate_91_to_92
    with closing(sqlite3.connect(':memory:')) as db:
        db.execute('CREATE TABLE thread_vendor_accounts (thread_id TEXT, provider TEXT, account_id TEXT)')

        migrate_91_to_92(db)
        migrate_91_to_92(db)

        db.execute("INSERT INTO thread_vendor_accounts VALUES ('t','codex','a',NULL)")
        db.execute("INSERT INTO thread_vendor_accounts VALUES ('t','claude-code','b','{}')")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO thread_vendor_accounts VALUES ('t','codex','c','not-json')")
