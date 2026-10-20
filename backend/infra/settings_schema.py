"""DDL for persistent backend settings."""

SETTINGS_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
"""
