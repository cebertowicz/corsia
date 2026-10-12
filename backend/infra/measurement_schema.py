"""Indexes the Measurement dashboard aggregates range over.

One definition serves both the fresh schema and the v77 -> v78 migration, so
fresh and migrated databases carry identical indexes.
"""

from __future__ import annotations


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
    "artifact_identities": """
    CREATE INDEX IF NOT EXISTS idx_measurement_decisions_thread
        ON artifact_identities(json_extract(metadata, '$.plan_decision.thread_id'))
        WHERE integration='local' AND type='plan-decision' AND json_valid(metadata)""",
}

MEASUREMENT_INDEX_SQL = "".join(f"{sql};\n" for sql in MEASUREMENT_INDEXES.values())
