"""Schema fragment for persisted System bulletin delivery and dispositions."""

BULLETIN_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS system_bulletin_identity (
        id INTEGER PRIMARY KEY CHECK(id=1), version TEXT NOT NULL,
        digest TEXT NOT NULL, files_json TEXT NOT NULL, reconciled_at TEXT
    );
    CREATE TABLE IF NOT EXISTS system_bulletins (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL CHECK(source IN ('system_change','live')),
        state TEXT NOT NULL CHECK(state IN ('pending','skipped','delivered','acted','not_applicable','deferred','withdrawn')),
        disposition TEXT, delivery_id TEXT, delivered_at TEXT, acted_at TEXT, withdrawn_at TEXT, updated_at TEXT NOT NULL
    );
"""
