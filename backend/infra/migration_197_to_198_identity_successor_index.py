"""Schema 197 -> 198: index each identity by the successor its promote handed it to.

A retired document's comments live on the identity its promote handed it to, so a
read of a document asks which identities name it as their successor
(``registry.predecessors``). Without this index that question scans every
identity. The index is partial and covers only identities a promote retired.
Adds the index only; no row changes, and a second run does nothing. Runs inside
the runner's transaction; it never commits.
"""

from __future__ import annotations

import logging
import sqlite3
import time

from infra.artifact_identity_schema import SUCCESSOR_INDEX_SQL


def migrate_197_to_198(conn: sqlite3.Connection) -> None:
    """Create the successor index on an existing identity table."""
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifact_identities'"
    ).fetchone() is None:
        return
    started = time.perf_counter()
    conn.execute(SUCCESSOR_INDEX_SQL.strip().rstrip(";"))
    elapsed_ms = (time.perf_counter() - started) * 1000
    logging.getLogger("infra.database_migrations").info(
        "Built artifact identity successor index: elapsed_ms=%.1f", elapsed_ms
    )
