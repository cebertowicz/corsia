"""Indexes the Measurement dashboard aggregates range over.

One definition serves both the fresh schema and the v77 -> v78 migration, so
fresh and migrated databases carry identical indexes.
"""

from __future__ import annotations

from core.measurement_token_contract import priceable_usage_predicate


MEASUREMENT_BREAKS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS measure_breaks (
    period TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('start','world','source')),
    metric_group TEXT,
    reason TEXT NOT NULL,
    PRIMARY KEY(period, kind, metric_group)
);
CREATE TABLE IF NOT EXISTS measurement_model_versions (
    identity TEXT PRIMARY KEY,
    started_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_measure_breaks_identity
    ON measure_breaks(period, kind, COALESCE(metric_group, ''));
CREATE UNIQUE INDEX IF NOT EXISTS idx_measure_break_starts_group
    ON measure_breaks(metric_group) WHERE kind='start';
"""


MEASUREMENT_INDEXES: dict[str, str] = {
    # Every scope walks the thread tree from its roots; without this each
    # recursive step scans all threads. Partial, so the many root threads'
    # NULL parents cannot make the planner judge a child lookup unselective.
    "threads": """
    CREATE INDEX IF NOT EXISTS idx_measurement_thread_children
        ON threads(parent_thread_id, user_id) WHERE parent_thread_id IS NOT NULL""",
    # Recorded by the v77 -> v78 migration. Measurement no longer reads it
    # (attention is presence time); dropping it needs its own migration.
    "messages": """
    CREATE INDEX IF NOT EXISTS idx_measurement_human_messages_time
        ON messages(thread_id, created_at, id) WHERE role='user'""",
    "plan_node_events": """
    CREATE INDEX IF NOT EXISTS idx_measurement_rejections_time
        ON plan_node_events(created_at, node_id) WHERE event='review_rejected'""",
    # The user-owned hold boundary can be newly observed during a request, then
    # is retained with the other immutable measurement boundaries.
    "plan_node_holds": """
    CREATE INDEX IF NOT EXISTS idx_measurement_user_holds_start
        ON plan_node_holds(created_at) WHERE owner_kind='user'""",
    # The install-wide fresh-token boundary uses exactly this contract. Keeping
    # only qualifying rows makes its first retained record an index seek rather
    # than a traversal of the complete usage ledger.
    "llm_usage": f"""
    CREATE INDEX IF NOT EXISTS idx_llm_usage_priceable_measurement_start ON llm_usage(created_at)
    WHERE {priceable_usage_predicate()}""",
    "artifact_identities": """
    CREATE INDEX IF NOT EXISTS idx_measurement_decisions_thread
        ON artifact_identities(json_extract(metadata, '$.plan_decision.thread_id'))
        WHERE integration='local' AND type='plan-decision' AND json_valid(metadata)""",
    "decision_raised": """
    CREATE INDEX IF NOT EXISTS idx_measurement_decisions_raised
        ON artifact_identities(json_extract(metadata, '$.plan_decision.raised_at'))
        WHERE integration='local' AND type='plan-decision' AND json_valid(metadata)""",
    "decision_answered": """
    CREATE INDEX IF NOT EXISTS idx_measurement_decisions_answered
        ON artifact_identities(json_extract(metadata, '$.plan_decision.answered_at'))
        WHERE integration='local' AND type='plan-decision' AND json_valid(metadata)""",
    "first_try_failed": """
    CREATE INDEX IF NOT EXISTS idx_measurement_first_try_failed
        ON plan_node_events(created_at) WHERE new_state='failed'""",
    "first_try_corrections": """
    CREATE INDEX IF NOT EXISTS idx_measurement_first_try_corrections
        ON plan_node_events(created_at) WHERE event='correction_created'""",
    "first_try_released_bad": """
    CREATE INDEX IF NOT EXISTS idx_measurement_first_try_released_bad
        ON plan_node_attempts(released_at) WHERE release_reason IN ('failed','requirements_changed')""",
    "size_rater": """
    CREATE INDEX IF NOT EXISTS idx_measurement_size_rater
        ON outcome_events(json_extract(attributes, '$.rater'), subject_key)
        WHERE event_name='measurement.size_rating' AND retracted_at IS NULL
          AND json_extract(attributes, '$.rater') > ''""",
}

MEASUREMENT_INDEX_SQL = MEASUREMENT_BREAKS_SCHEMA_SQL + "".join(f"{sql};\n" for sql in MEASUREMENT_INDEXES.values())
