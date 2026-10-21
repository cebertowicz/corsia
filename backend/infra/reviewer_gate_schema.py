"""SQLite schema for the reviewer gate's per-pull-request receipts."""

REVIEWER_GATE_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS reviewer_gate_state (
        pr_key       TEXT PRIMARY KEY,
        reviewed_sha TEXT NOT NULL
    );
    -- The head a maintainer approved for review; a fork head is reviewable
    -- only while it equals the pull request's current head.
    CREATE TABLE IF NOT EXISTS reviewer_gate_approval (
        pr_key       TEXT PRIMARY KEY,
        approved_sha TEXT NOT NULL
    );
"""
