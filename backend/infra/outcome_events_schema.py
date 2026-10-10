"""The outcome event table: durable facts about outcomes from outside CORSIA.

One row per source-stable fact, keyed on ``dedupe_key`` so every adapter is
idempotent. ``attributes`` holds only allowlisted ids, counts and enums (see
``infra.outcome_event_attributes``); a withdrawn row keeps a tombstone, never
its content. ``outcome_event_counters`` holds the durable intake counters
(duplicates ignored, attribute keys dropped) that measures read later.
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
    CREATE TABLE IF NOT EXISTS outcome_event_counters (
        counter    TEXT NOT NULL CHECK (counter IN (
                     'duplicate_ignored', 'attribute_dropped', 'mapping_failed')),
        event_name TEXT NOT NULL,
        n          INTEGER NOT NULL DEFAULT 0 CHECK (n >= 0),
        PRIMARY KEY (counter, event_name)
    );
"""
