"""Schema 77 -> 78: give existing databases the Measurement query indexes.

Purely additive: indexes only, no row or column changes. Runs inside the
runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3


# Frozen at schema v78. This historical step must never inherit a later
# Measurement index whose table or columns did not exist at v77.
_V78_MEASUREMENT_INDEXES = {
    "threads": """
    CREATE INDEX IF NOT EXISTS idx_measurement_thread_children
        ON threads(parent_thread_id, user_id) WHERE parent_thread_id IS NOT NULL""",
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


def migrate_77_to_78(conn: sqlite3.Connection) -> None:
    """Create each frozen v78 index whose table the database contains."""
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, sql in _V78_MEASUREMENT_INDEXES.items():
        if table in tables:
            conn.execute(sql)
