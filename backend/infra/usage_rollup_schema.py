"""Storage for the hourly ``llm_usage`` rollup, and the one rule that keeps it exact.

The rollup is a cache of ``llm_usage`` grouped by UTC hour, built by
``api.usage_rollup``. Its tables are defined here, beside the ledger writer that
marks them, so ``infra`` never imports the Measurement SQL that fills them.

``GRACE`` is the one bound that keeps the rollup exact. The rollup never covers
an hour later than ``now - GRACE``, so any row written with a later
``created_at`` lands in an hour that is still read raw. A row written with an
earlier ``created_at`` (a late receipt, a startup reconcile, retained usage)
marks its hour dirty in the writer's own transaction; dirty hours are read raw
until a background refresh rebuilds them. Nothing here rebuilds anything.

A writer marks against a limit ``MARK_LEAD`` later than the build's. A build
reads outside any transaction, so a writer that checked just before the limit
ticked can commit after a build read the hour without its row. The lead covers
that gap unless one write transaction stays open longer than ``MARK_LEAD``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

GRACE = timedelta(hours=2)
MARK_LEAD = GRACE / 2

USAGE_ROLLUP_TABLES: dict[str, str] = {
    "llm_usage_hourly": """
    CREATE TABLE IF NOT EXISTS llm_usage_hourly (
        hour TEXT NOT NULL, thread_id TEXT, model TEXT, source TEXT,
        context_manifest_id TEXT, vendor_account_id TEXT,
        admission TEXT NOT NULL, input_tier TEXT NOT NULL, idle_hours INTEGER NOT NULL,
        records INTEGER NOT NULL, prompt_tokens INTEGER, completion_tokens INTEGER,
        cache_creation_tokens INTEGER, cache_read_tokens INTEGER, total_tokens INTEGER,
        fresh_tokens INTEGER, fresh_unknown_records INTEGER,
        cost_usd REAL, cost_records INTEGER NOT NULL, first_at TEXT NOT NULL
    )""",
    "idx_llm_usage_hourly_hour": """
    CREATE INDEX IF NOT EXISTS idx_llm_usage_hourly_hour ON llm_usage_hourly(hour, thread_id)""",
    # ``seq`` grows on every re-mark, so a refresh clears only the mark it rebuilt.
    "llm_usage_hourly_dirty": """
    CREATE TABLE IF NOT EXISTS llm_usage_hourly_dirty (
        hour TEXT PRIMARY KEY, seq INTEGER NOT NULL DEFAULT 1
    )""",
    # ``built_through`` is an exclusive hour key; ``definition`` names the
    # grouping that built the rows, so a changed grouping rebuilds lazily.
    "llm_usage_hourly_state": """
    CREATE TABLE IF NOT EXISTS llm_usage_hourly_state (
        id INTEGER PRIMARY KEY CHECK (id = 1), definition TEXT NOT NULL, built_through TEXT NOT NULL
    )""",
    # The rollup's builds and raw tails range over created_at.
    "idx_llm_usage_created_at": """
    CREATE INDEX IF NOT EXISTS idx_llm_usage_created_at ON llm_usage(created_at)""",
}

USAGE_ROLLUP_SQL = "".join(f"{sql};\n" for sql in USAGE_ROLLUP_TABLES.values())


def hour_key(created_at: str) -> str:
    """The hour a stored ``created_at`` belongs to: its first 13 characters."""
    return created_at[:13]


def utc_now() -> datetime:
    """Naive UTC now, the shape ``llm_usage.created_at`` is stored in."""
    return datetime.now(UTC).replace(tzinfo=None)


def grace_limit(now: datetime | None = None) -> str:
    """The first hour the rollup may not cover yet."""
    return hour_key(((now or utc_now()) - GRACE).isoformat())


def mark_late_insert(db: Any, created_at: str, now: datetime | None = None) -> None:
    """Mark ``created_at``'s hour dirty when the rollup may already cover it.

    Runs inside the ledger writer's transaction and only marks: the rebuild is
    a later background refresh, never this transaction's work.
    """
    hour = hour_key(created_at)
    if hour >= grace_limit((now or utc_now()) + MARK_LEAD):
        return
    db.execute(
        "INSERT INTO llm_usage_hourly_dirty(hour) VALUES (?) "
        "ON CONFLICT(hour) DO UPDATE SET seq = seq + 1",
        (hour,),
    )
