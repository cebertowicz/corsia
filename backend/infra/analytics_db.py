"""DuckDB-backed analytical store for CORSIA product events.

The analytics DB is separate from the OLTP SQLite DB so percentile/group-by
workloads cannot block the request path. The schema bootstraps idempotently on
first open and uses one process-wide DuckDB connection.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from infra.config import ANALYTICS_DB_PATH

if TYPE_CHECKING:
    import duckdb

log = logging.getLogger(__name__)

_conn: "duckdb.DuckDBPyConnection | None" = None
_init_lock = threading.Lock()

_BOOTSTRAP_DDL = """
CREATE TABLE IF NOT EXISTS product_events (
    ts TIMESTAMP NOT NULL,
    category VARCHAR NOT NULL,
    action VARCHAR NOT NULL,
    properties JSON,
    session_id VARCHAR,
    project_id VARCHAR
);
CREATE INDEX IF NOT EXISTS idx_pe_cat_act_ts ON product_events (category, action, ts);
CREATE INDEX IF NOT EXISTS idx_pe_proj_ts ON product_events (project_id, ts);
CREATE TABLE IF NOT EXISTS request_timing_daily (
    day DATE NOT NULL,
    method VARCHAR NOT NULL,
    route VARCHAR NOT NULL,
    status_class VARCHAR NOT NULL,
    count BIGINT NOT NULL,
    sum_ms DOUBLE NOT NULL,
    max_ms DOUBLE NOT NULL,
    sum_bytes BIGINT,
    max_bytes BIGINT,
    buckets INTEGER[] NOT NULL,
    PRIMARY KEY (day, method, route, status_class)
);
CREATE TABLE IF NOT EXISTS request_slow (
    ts TIMESTAMP NOT NULL,
    method VARCHAR NOT NULL,
    route VARCHAR NOT NULL,
    status INTEGER NOT NULL,
    dur_ms DOUBLE NOT NULL,
    dbq_ms DOUBLE NOT NULL,
    db_ms DOUBLE NOT NULL,
    response_bytes BIGINT
);
-- Response bytes are nullable with no default: rows written before byte measurement
-- began are unknown, never zero.
ALTER TABLE request_timing_daily ADD COLUMN IF NOT EXISTS sum_bytes BIGINT;
ALTER TABLE request_timing_daily ADD COLUMN IF NOT EXISTS max_bytes BIGINT;
ALTER TABLE request_slow ADD COLUMN IF NOT EXISTS response_bytes BIGINT;
-- Per-lane DB wait and work. Nullable with no default: a row written before the
-- lanes existed did not measure them, so it reads as unknown, never as zero.
ALTER TABLE request_slow ADD COLUMN IF NOT EXISTS dbq_read_ms DOUBLE;
ALTER TABLE request_slow ADD COLUMN IF NOT EXISTS db_read_ms DOUBLE;
ALTER TABLE request_slow ADD COLUMN IF NOT EXISTS dbq_bulk_ms DOUBLE;
ALTER TABLE request_slow ADD COLUMN IF NOT EXISTS db_bulk_ms DOUBLE;
CREATE INDEX IF NOT EXISTS idx_request_slow_ts ON request_slow (ts);
"""


def get_db() -> "duckdb.DuckDBPyConnection":
    """Return the process-wide DuckDB connection, bootstrapping on first call."""
    global _conn
    if _conn is not None:
        return _conn
    with _init_lock:
        if _conn is not None:  # second writer lost the race
            return _conn
        import duckdb

        ANALYTICS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        log.info("opening analytics DB at %s", ANALYTICS_DB_PATH)
        conn = duckdb.connect(str(ANALYTICS_DB_PATH))
        conn.execute(_BOOTSTRAP_DDL)
        _conn = conn
    return _conn


def close() -> None:
    """Close the singleton connection if it is open."""
    global _conn
    if _conn is None:
        return
    try:
        _conn.close()
    except Exception as exc:  # noqa: BLE001  # silent-swallow: shutdown path
        log.warning("error closing analytics db: %s", exc)
    finally:
        _conn = None


def _reset_for_tests() -> None:
    """Drop the singleton so tests can open a different path."""
    close()
