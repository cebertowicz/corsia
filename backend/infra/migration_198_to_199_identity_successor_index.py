"""Schema 198 -> 199: index each identity by the successor its promote handed it to.

A promote moves a document's comments to the identity it hands the document to, so new
comments never stay on a retired identity. Older rows can: promotes made before that move
existed, and comments an old pane saved on a retired id. A read of the document asks which
identities name it as their successor (``registry.predecessors``) to find them. That read is
permanent: nothing moves rows off an identity that is already retired, because it would relocate
people's comments on existing installs and needs its own design.
Without this index that question scans every identity. The index is partial and covers only identities a promote retired.
Adds the index only; no row changes, and a second run does nothing. Runs inside
the runner's transaction; it never commits.
"""

from __future__ import annotations

import logging
import sqlite3
import time

from infra.artifact_identity_schema import SUCCESSOR_INDEX_SQL


def migrate_198_to_199(conn: sqlite3.Connection) -> None:
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
