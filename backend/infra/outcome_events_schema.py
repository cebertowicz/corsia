"""The outcome event table: durable facts about outcomes from outside CORSIA.

One row per source-stable fact, keyed on ``dedupe_key`` so every adapter is
idempotent. ``attributes`` holds only allowlisted ids, counts and enums (see
``infra.outcome_event_attributes``); a withdrawn row keeps a tombstone, never
its content. ``outcome_event_counters`` holds the durable intake counters
(duplicates ignored, attribute keys dropped) that measures read later, keyed agent, day,
event, counter so an agent's recent window is one index range.
``infra.outcome_events`` is the only writer; measures read the table in their own queries.
"""

from __future__ import annotations

OUTCOME_EVENTS_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS outcome_events (
        id              TEXT PRIMARY KEY,
        dedupe_key      TEXT NOT NULL UNIQUE CHECK (length(trim(dedupe_key)) > 0),
        event_name      TEXT NOT NULL CHECK (length(trim(event_name)) > 0),
        source          TEXT NOT NULL
                          CHECK (source IN ('github', 'corsia', 'agent', 'otlp')),
        provenance      TEXT NOT NULL
                          CHECK (provenance IN (
                            'observed', 'imported', 'instrumented', 'agent_recorded')),
        ts              TEXT NOT NULL,
        observed_ts     TEXT NOT NULL,
        subject_key     TEXT,
        project_id      TEXT,
        agent_thread_id TEXT,
        attribution     TEXT CHECK (attribution IS NULL OR attribution IN (
                          'title_prefix', 'body_owner', 'branch', 'caller', 'none')),
        corroborated_by TEXT,
        attributes      TEXT NOT NULL
                          CHECK (json_valid(attributes)
                                 AND json_type(attributes) = 'object'),
        retracted_at    TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_outcome_events_name_ts
        ON outcome_events(event_name, ts);
    CREATE INDEX IF NOT EXISTS idx_outcome_events_agent_ts
        ON outcome_events(agent_thread_id, ts);
    CREATE INDEX IF NOT EXISTS idx_outcome_events_project_ts
        ON outcome_events(project_id, ts);
    CREATE INDEX IF NOT EXISTS idx_outcome_events_subject
        ON outcome_events(subject_key);
    CREATE INDEX IF NOT EXISTS idx_outcome_events_agent_observed
        ON outcome_events(agent_thread_id, observed_ts, event_name)
        WHERE agent_thread_id IS NOT NULL;
    CREATE TABLE IF NOT EXISTS outcome_event_counters (
        counter TEXT NOT NULL CHECK (counter IN (
            'duplicate_ignored', 'attribute_dropped', 'mapping_failed',
            'name_undeclared', 'namespace_not_owned',
            'rate_limited', 'record_refused', 'unversioned')),
        event_name TEXT NOT NULL,
        agent_thread_id TEXT NOT NULL DEFAULT '',
        day TEXT NOT NULL DEFAULT '0000-00-00',
        n INTEGER NOT NULL DEFAULT 0 CHECK (n >= 0),
        PRIMARY KEY (agent_thread_id, day, event_name, counter)
    );
    CREATE TABLE IF NOT EXISTS measure_definitions (
        owner_thread_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('event', 'kpi', 'view')),
        name TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version > 0),
        spec_json TEXT NOT NULL CHECK (json_valid(spec_json)),
        file_path TEXT NOT NULL,
        file_sha256 TEXT NOT NULL,
        synced_at TEXT NOT NULL,
        removed INTEGER NOT NULL DEFAULT 0 CHECK (removed IN (0, 1)),
        PRIMARY KEY (owner_thread_id, kind, name, version)
    );
    CREATE TABLE IF NOT EXISTS measure_namespaces (
        namespace TEXT PRIMARY KEY,
        owner_thread_id TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS measure_workflows (
        workflow TEXT PRIMARY KEY,
        owner_thread_id TEXT NOT NULL
    );
"""
