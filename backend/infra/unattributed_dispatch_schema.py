"""Schema for the per-Turn unattributed-work-call measurement table.

Its own module under the one-module-per-durable-domain convention, composed
into the Plans schema by ``infra.plan_schema``.

Temporary diagnosis, not a permanent surface: it exists to decide whether
agents running work calls with no plan task selected need a reminder, and
whether that reminder moves the number. Delete the table, its writer in
``plans.tool_dispatches`` and this module once that decision is settled;
reviewed at each Plans release.
"""

from __future__ import annotations

UNATTRIBUTED_DISPATCH_SCHEMA_SQL = """
    -- One row per Turn that ran at least one work-tool call with no plan task
    -- selected (``plans.tool_dispatches``). Written once, at Turn terminal,
    -- from an in-process counter: a mid-Turn process restart or a failed
    -- write loses that Turn, so totals here are a lower bound, never exact.
    -- Like ``plan_tool_dispatches``, a row is audit data that outlives its
    -- thread: nothing deletes plan records with a thread, so ``thread_id``
    -- declares no foreign key it could not keep.
    CREATE TABLE IF NOT EXISTS plan_unattributed_dispatches (
        turn_id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL,
        call_count INTEGER NOT NULL CHECK (call_count > 0),
        plan_state TEXT NOT NULL CHECK (plan_state IN ('active', 'paused', 'none')),
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_plan_unattributed_dispatches_thread
        ON plan_unattributed_dispatches(thread_id, created_at);
"""
