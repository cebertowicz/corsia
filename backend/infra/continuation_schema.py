"""Continuation trigger DDL, split out of ``infra.schema``."""

CONTINUATION_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS continuation_triggers (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN (
            't0_liveness', 't1_watchdog', 't2_watch_expiry',
            'restart_resume', 'automation_clock', 'plan_parent'
        )),
        thread_id TEXT NOT NULL REFERENCES threads(id),
        scope_id TEXT NOT NULL,
        subject_id TEXT,
        dedupe_key TEXT NOT NULL,
        idempotency_key TEXT,
        state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN (
            'pending', 'dispatched', 'delivered', 'deferred', 'suppressed',
            'stalled', 'cancelled', 'failed'
        )),
        reason TEXT,
        attempt INTEGER NOT NULL DEFAULT 0,
        deferrals INTEGER NOT NULL DEFAULT 0,
        cost_estimate_usd REAL NOT NULL DEFAULT 0.0,
        next_attempt_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        dispatched_at TEXT,
        settled_at TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_continuation_triggers_live
        ON continuation_triggers(dedupe_key)
        WHERE state IN ('pending', 'dispatched', 'deferred');
    CREATE INDEX IF NOT EXISTS idx_continuation_triggers_state
        ON continuation_triggers(state, next_attempt_at);
    CREATE INDEX IF NOT EXISTS idx_continuation_triggers_scope
        ON continuation_triggers(scope_id, created_at);

    CREATE TABLE IF NOT EXISTS automation_schedules (
        id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL REFERENCES threads(id),
        scope_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('clock', 'cadence')),
        spec TEXT NOT NULL,
        next_run_at TEXT NOT NULL,
        last_run_at TEXT,
        runs INTEGER NOT NULL DEFAULT 0,
        max_runs INTEGER NOT NULL CHECK (max_runs > 0),
        state TEXT NOT NULL DEFAULT 'active'
            CHECK (state IN ('active', 'paused', 'cancelled', 'exhausted')),
        payload_json TEXT,
        created_by_actor TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_automation_schedules_due
        ON automation_schedules(state, next_run_at);
    CREATE INDEX IF NOT EXISTS idx_automation_schedules_scope
        ON automation_schedules(scope_id, next_run_at);
"""
