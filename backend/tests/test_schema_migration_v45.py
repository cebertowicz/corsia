"""Schema v45 records the check suite of check-suite deliveries."""

from __future__ import annotations

from tests.schema_rewind import rewind_to_v88_policy

from contextlib import closing
import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION
from infra.webhook_delivery_log import LAST_WITHOUT_ID_SQL


def _has_check_suite_id(db) -> bool:
    columns = {str(row[1]) for row in db.execute("PRAGMA table_info(webhook_deliveries)")}
    indexes = {str(row[1]) for row in db.execute("PRAGMA index_list(webhook_deliveries)")}
    return "check_suite_id" in columns and "idx_webhook_deliveries_check_suite" in indexes


def test_fresh_schema_has_check_suite_id():
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        assert SCHEMA_VERSION == 148
        assert _has_check_suite_id(db)


def test_startup_migration_adds_check_suite_id_and_drops_ingress_receipts(tmp_path):
    path = tmp_path / "legacy-webhook-deliveries-v44.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute("DROP INDEX idx_webhook_deliveries_check_suite")
        db.execute("ALTER TABLE webhook_deliveries DROP COLUMN check_suite_id")
        db.execute(
            "INSERT INTO webhook_deliveries (id,event_type,action,delivery_id,payload_hash,"
            "status,created_at) VALUES ('old','check_suite','completed','old','h','dispatched','t')"
        )
        db.executemany(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, '{}', 't')",
            [("pr_ingress:o/r#1",), ("pr_quiet:o/r#1",)],
        )
        rewind_to_v88_policy(db)

        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=44")
        db.commit()

    with closing(sqlite3.connect(path)) as db:
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (True, 44, SCHEMA_VERSION)
        assert _has_check_suite_id(db)
        assert db.execute(
            "SELECT status, check_suite_id FROM webhook_deliveries WHERE id='old'"
        ).fetchone() == ("dispatched", None)
        assert [row[0] for row in db.execute(
            "SELECT key FROM settings WHERE key LIKE 'pr_%' ORDER BY key"
        )] == ["pr_quiet:o/r#1"]
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated


def test_receipt_lookups_use_the_indexes():
    """The sweep's two log reads stay bounded as the log grows (EAS21)."""
    with closing(sqlite3.connect(":memory:")) as db:
        initialize_schema(db)
        per_suite = " ".join(str(row[-1]) for row in db.execute(
            "EXPLAIN QUERY PLAN SELECT check_suite_id, MIN(created_at) FROM webhook_deliveries "
            "WHERE event_type='check_suite' AND action='completed' "
            "AND ((check_suite_id=? AND created_at>=?)) GROUP BY check_suite_id", (1, "t"),
        ))
        since = " ".join(str(row[-1]) for row in db.execute(
            f"EXPLAIN QUERY PLAN {LAST_WITHOUT_ID_SQL}",
        ))
    assert "idx_webhook_deliveries_check_suite" in per_suite
    assert "idx_webhook_deliveries_event" in since and "TEMP B-TREE" not in since
    assert "SCAN webhook_deliveries" not in since, "never a full-table scan"
