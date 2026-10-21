"""Retain the reserved schema version; review feedback uses ordinary batches."""

from __future__ import annotations

import sqlite3


def migrate_133_to_134(conn: sqlite3.Connection) -> None:
    """No schema change: review forks need no additional feedback storage."""
