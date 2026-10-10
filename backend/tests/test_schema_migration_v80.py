from __future__ import annotations

import sqlite3

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION


def test_schema_79_adds_the_bulletin_ledger_matching_a_fresh_install() -> None:
    fresh = sqlite3.connect(":memory:")
    db = sqlite3.connect(":memory:")
    try:
        initialize_schema(fresh)
        expected = {
            name: fresh.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,),
            ).fetchone()[0]
            for name in ("system_bulletins", "system_bulletin_identity")
        }
        initialize_schema(db)
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("PRAGMA user_version=79")
        db.commit()
        result = migrate_to_current(db, 80)
        assert (result.migrated, result.from_version, result.to_version) == (True, 79, 80)
        actual = {
            name: db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,),
            ).fetchone()[0]
            for name in expected
        }
        assert actual == expected
        columns = {row[1] for row in db.execute("PRAGMA table_info(system_bulletins)")}
        assert {"delivery_id", "withdrawn_at"} <= columns
        db.execute(
            "INSERT INTO system_bulletins(id,source,state,delivered_at,updated_at) VALUES(?,?,?,?,?)",
            ("pending", "live", "pending", None, "now"),
        )
        assert db.execute("SELECT state FROM system_bulletins WHERE id='pending'").fetchone()[0] == "pending"
    finally:
        fresh.close()
        db.close()
