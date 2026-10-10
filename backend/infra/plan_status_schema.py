"""Schema for the per-plan marker that a plan's status still needs journaling."""

PLAN_STATUS_DIRTY_SCHEMA_SQL = """
    -- One row per thread plan whose plan.status projection is not journaled
    -- yet. A plan write replaces the row in its own transaction, so it takes a
    -- fresh dirty_seq. AUTOINCREMENT never reuses one, so a drain that read
    -- seq S clears the row only if no later write replaced it. (The migration
    -- splits this text on semicolons, so none may appear in a comment.)
    CREATE TABLE IF NOT EXISTS plan_status_dirty (
        dirty_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id TEXT NOT NULL UNIQUE
    );
"""
