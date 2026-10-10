"""Append-only history of subscription limit-window observations."""

from __future__ import annotations

#: One row per change of a window reading. ``window_key`` is the stored
#: window identity (vendor, meter, kind, scope); ``window_kind`` is its kind
#: (``five_hour``, ``seven_day``, ...). The index serves one account and one
#: window between two times.
LIMIT_HISTORY_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS limit_observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id TEXT NOT NULL REFERENCES vendor_accounts(id),
        window_key TEXT NOT NULL,
        window_kind TEXT NOT NULL,
        used_percent REAL,
        resets_at TEXT,
        source TEXT NOT NULL,
        observed_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_limit_observations_window
        ON limit_observations(account_id, window_key, observed_at);
"""
