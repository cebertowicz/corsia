"""Canonical persisted presence timestamps and their seen boundary."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime


def iso(value: datetime) -> str:
    """Render a naive-UTC datetime in the stored shape, always with microseconds."""
    return value.isoformat(timespec="microseconds")


def parse(value: str) -> datetime:
    """Parse a stored timestamp to naive UTC; an explicit offset is converted."""
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo is not None else parsed


def seen_boundary(row: sqlite3.Row | None) -> str | None:
    """The committed timestamp through which a presence row is seen."""
    if row is None or (row["present"] and row["exit_pending_at"] is None):
        return None
    left = row["exit_pending_at"] if row["present"] else row["last_exited_at"]
    marks = [iso(parse(value)) for value in (left, row["marked_read_at"]) if value]
    return max(marks) if marks else None
