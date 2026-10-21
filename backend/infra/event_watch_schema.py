"""Expected-event watch DDL, split out of ``infra.schema``."""

EXPECTED_EVENT_WATCH_SCHEMA_SQL = """
    -- Reliability automations (coordination MVP S7): expected-event watches arm
    -- from concrete conditions, persist, resolve once, expire once. The dedupe
    -- index keys an armed watch as one thread's wait on one artifact's event.
    CREATE TABLE IF NOT EXISTS expected_event_watches (
        id TEXT PRIMARY KEY,
        artifact_integration TEXT,
        artifact_type TEXT,
        artifact_external_id TEXT,
        owner_thread_id TEXT,
        plan_id TEXT REFERENCES plans(id),
        plan_node_id TEXT REFERENCES plan_nodes(id),
        armed_by_actor_id TEXT,
        expected_event TEXT,
        condition_type TEXT NOT NULL,
        condition_value TEXT,
        status TEXT NOT NULL DEFAULT 'armed',
        armed_at TEXT NOT NULL,
        resolved_at TEXT,
        expired_at TEXT,
        routed_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_watches_status
        ON expected_event_watches(status, condition_type);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_watches_dedupe ON expected_event_watches
        (artifact_integration, artifact_type, artifact_external_id, owner_thread_id,
         condition_type, condition_value, expected_event) WHERE status='armed';
"""
