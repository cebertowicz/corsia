"""Schema for the single durable per-thread plan liveness wake."""

PLAN_LIVENESS_SCHEMA_SQL = """
    -- A terminal Turn with actionable plan work arms one row per thread.
    -- Reuse the row across generic cycles and known provider-reset deadlines.
    CREATE TABLE IF NOT EXISTS plan_liveness_wakes (
        id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL DEFAULT 'armed',
        deadline TEXT NOT NULL,
        provider_reset_at TEXT,
        provider_reset_turn_id TEXT,
        cycle INTEGER NOT NULL DEFAULT 0,
        last_open_count INTEGER NOT NULL DEFAULT 0,
        last_signal_hash TEXT NOT NULL DEFAULT '',
        last_settled_events INTEGER NOT NULL DEFAULT 0,
        wakes_in_window INTEGER NOT NULL DEFAULT 0,
        window_started_at TEXT,
        armed_at TEXT NOT NULL,
        fired_at TEXT,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_plan_liveness_thread_status
        ON plan_liveness_wakes(thread_id, status);
"""
