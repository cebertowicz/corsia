"""Indexes the current Measurement reads range over."""

from __future__ import annotations

from core.measurement_token_contract import priceable_usage_predicate


MEASUREMENT_BREAKS_SCHEMA_SQL = """
-- period: a UTC instant for kind='start', bucketed in the viewer's zone at read time;
-- a calendar date for world and source marks.
CREATE TABLE IF NOT EXISTS measure_breaks (
    period TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('start','world','source')),
    metric_group TEXT,
    reason TEXT NOT NULL,
    PRIMARY KEY(period, kind, metric_group)
);
-- Model boundaries derive from rating facts at read time.
CREATE UNIQUE INDEX IF NOT EXISTS idx_measure_breaks_identity
    ON measure_breaks(period, kind, COALESCE(metric_group, ''));
CREATE UNIQUE INDEX IF NOT EXISTS idx_measure_break_starts_group
    ON measure_breaks(metric_group) WHERE kind='start';
"""


MEASUREMENT_INDEXES = (
    # Every scope walks the thread tree from its roots; without this each
    # recursive step scans all threads. Partial, so the many root threads'
    # NULL parents cannot make the planner judge a child lookup unselective.
    """
    CREATE INDEX IF NOT EXISTS idx_measurement_thread_children
        ON threads(parent_thread_id, user_id) WHERE parent_thread_id IS NOT NULL""",
    # Recorded by the v77 -> v78 migration. Measurement no longer reads it
    # (attention is presence time); dropping it needs its own migration.
    """
    CREATE INDEX IF NOT EXISTS idx_measurement_human_messages_time
        ON messages(thread_id, created_at, id) WHERE role='user'""",
    """
    CREATE INDEX IF NOT EXISTS idx_measurement_rejections_time
        ON plan_node_events(created_at, node_id) WHERE event='review_rejected'""",
    # The install-wide fresh-token boundary uses exactly this contract. Keeping
    # only qualifying rows makes its first retained record an index seek rather
    # than a traversal of the complete usage ledger.
    f"""
    CREATE INDEX IF NOT EXISTS idx_llm_usage_priceable_measurement_start ON llm_usage(created_at)
    WHERE {priceable_usage_predicate()}""",
    """
    CREATE INDEX IF NOT EXISTS idx_measurement_decisions_thread
        ON artifact_identities(json_extract(metadata, '$.plan_decision.thread_id'))
        WHERE integration='local' AND type='plan-decision' AND json_valid(metadata)""",
    """
    CREATE INDEX IF NOT EXISTS idx_measurement_decisions_raised
        ON artifact_identities(json_extract(metadata, '$.plan_decision.raised_at'))
        WHERE integration='local' AND type='plan-decision' AND json_valid(metadata)""",
    """
    CREATE INDEX IF NOT EXISTS idx_measurement_decisions_answered
        ON artifact_identities(json_extract(metadata, '$.plan_decision.answered_at'))
        WHERE integration='local' AND type='plan-decision' AND json_valid(metadata)""",
    """
    CREATE INDEX IF NOT EXISTS idx_measurement_size_rater
        ON outcome_events(json_extract(attributes, '$.rater'), subject_key)
        WHERE event_name='measurement.size_rating' AND retracted_at IS NULL
          AND json_extract(attributes, '$.rater') > ''""",
)

MEASUREMENT_INDEX_SQL = "".join(f"{sql};\n" for sql in MEASUREMENT_INDEXES)
