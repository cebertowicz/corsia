"""Durable ordering and nudge state for the person's activity queue."""

ACTIVITY_RANKING_SQL = """
CREATE TABLE IF NOT EXISTS activity_ranks (
    user_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    item_key TEXT NOT NULL,
    position INTEGER NOT NULL,
    reason TEXT NOT NULL,
    ranked_by TEXT NOT NULL,
    ranked_at TEXT NOT NULL,
    PRIMARY KEY (user_id, scope, item_key)
);
CREATE TABLE IF NOT EXISTS activity_rank_nudges (
    user_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    last_enqueued_at TEXT NOT NULL,
    PRIMARY KEY (user_id, scope)
);
"""
