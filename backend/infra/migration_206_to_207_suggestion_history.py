"""Schema 206 -> 207: bind non-live suggestion decisions to their document states."""

import logging
import sqlite3
import time

from infra.suggestion_history_schema import SUGGESTION_HISTORY_SCHEMA_SQL

log = logging.getLogger(__name__)


def migrate_206_to_207(db: sqlite3.Connection) -> None:
    """Create the receipt table in the migration runner's transaction."""
    started = time.monotonic()
    db.execute(SUGGESTION_HISTORY_SCHEMA_SQL)
    total = db.execute("SELECT COUNT(*) FROM doc_suggestion_history").fetchone()[0]
    log.info("suggestion history migration: rows changed=0 total=%d elapsed_ms=%.1f", total, (time.monotonic() - started) * 1000)
