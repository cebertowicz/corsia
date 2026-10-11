"""Overwrite, not just unlink, the credential text a migration step strips from the database."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def secure_delete(conn: sqlite3.Connection) -> Iterator[None]:
    """Zero freed database content while the block runs, then restore the caller's setting.

    ``PRAGMA secure_delete`` may change inside the runner's open transaction, so a
    step wraps just the UPDATEs that remove a credential from a row.
    """
    previous = int(conn.execute("PRAGMA secure_delete").fetchone()[0])
    conn.execute("PRAGMA secure_delete = ON")
    try:
        yield
    finally:
        conn.execute(f"PRAGMA secure_delete = {previous}")
