"""Stored inference model profiles."""

MODEL_PROFILES_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS model_profiles (
        model_id TEXT PRIMARY KEY,
        context_window INTEGER,
        temperature REAL,
        parallel_slots INTEGER DEFAULT 1,
        reasoning_budget INTEGER DEFAULT -1,
        flash_attention INTEGER DEFAULT 1,
        custom_args TEXT,
        max_tokens INTEGER,
        updated_at TEXT
    );
"""
