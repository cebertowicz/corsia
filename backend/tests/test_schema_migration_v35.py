"""Schema v35 adds the cross-source object key to the webhook delivery log."""

from __future__ import annotations

from contextlib import closing
import sqlite3

import pytest

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION
from tests.schema_rewind import rewind_below_v42, rewind_to_v88_policy


def _has_object_key(db) -> bool:
    columns = {str(row[1]) for row in db.execute("PRAGMA table_info(webhook_deliveries)")}
    indexes = {str(row[1]) for row in db.execute("PRAGMA index_list(webhook_deliveries)")}
    return "object_key" in columns and "idx_webhook_deliveries_object_key" in indexes


def _insert(db, delivery_id: str, object_key: str | None) -> None:
    db.execute(
        "INSERT INTO webhook_deliveries (id,event_type,delivery_id,payload_hash,"
        "status,created_at,object_key) VALUES (?,?,?,?,?,?,?)",
        (delivery_id, "issue_comment", delivery_id, "h", "received", "t", object_key),
    )


def test_fresh_schema_has_unique_object_key():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        assert SCHEMA_VERSION == 146
        assert _has_object_key(db)
        _insert(db, "a", None)
        _insert(db, "b", None)
        _insert(db, "c", "issue_comment:1")
        with pytest.raises(sqlite3.IntegrityError):
            _insert(db, "d", "issue_comment:1")


def test_startup_migration_adds_object_key_and_keeps_rows(tmp_path):
    path = tmp_path / "legacy-webhook-deliveries-v34.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute("DROP INDEX idx_webhook_deliveries_object_key")
        db.execute("ALTER TABLE webhook_deliveries DROP COLUMN object_key")
        db.execute(
            "INSERT INTO webhook_deliveries (id,event_type,delivery_id,payload_hash,"
            "status,created_at) VALUES ('old','issue_comment','old','h','dispatched','t')"
        )
        rewind_below_v42(db)
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=34")
        db.commit()

    with closing(sqlite3.connect(path)) as db:
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (True, 34, SCHEMA_VERSION)
        assert _has_object_key(db)
        assert db.execute(
            "SELECT status, object_key FROM webhook_deliveries WHERE id='old'"
        ).fetchone() == ("dispatched", None)
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
