"""SQLite DDL for the daily fact-extraction run record."""

FACT_EXTRACTION_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS fact_extraction_runs (
        run_date TEXT PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'ok',
        result_json TEXT,
        ran_at TEXT NOT NULL
    );
"""
