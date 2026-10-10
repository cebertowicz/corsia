"""The capped SQLite store for CORSIA's own log records.

A separate file (``otel_logs.db`` in the state root), never ``corsia.db``:
its write lock is not shared with the product database, and a bad logs file
can be deleted without touching anything else. Logs are evidence, not state,
so a schema-version mismatch recreates the file instead of migrating it.

Exactly one ``LogStore`` per file writes, guarded by an exclusive ``flock``.
Its connection disables autocheckpoint so the WAL grows only between the
explicit checkpoints the pre-write check runs. Before every insert the store
enforces, in order: the free-disk floor, the WAL threshold, then the soft size
limit (prune first; drop the batch if still over). This is design r5 §4's cap,
simplified to one CORSIA-only table; its footprint stays under ``max_bytes``.

Readers use ``read_records``, a separate ``mode=ro`` connection with a deadline.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import sqlite3
import sys
import time
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from infra.otel_logs.capture import CapturedRecord

SCHEMA_VERSION = "1"
PAGE_SIZE = 4096
DEFAULT_MAX_BYTES = 512 * 1024 * 1024
RETENTION_SECONDS = 14 * 24 * 3600
DISK_FLOOR_BYTES = (2 * 1024 + 56) * 1024 * 1024
WAL_THRESHOLD_BYTES = 16 * 1024 * 1024
PRUNE_CHUNK_ROWS = 256
READ_DEADLINE_SECONDS = 2.0

_SCHEMA = """
CREATE TABLE store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE log_records (
  seq             INTEGER PRIMARY KEY,
  ts_ns           INTEGER NOT NULL,
  received_ns     INTEGER NOT NULL,
  severity_number INTEGER NOT NULL,
  severity_text   TEXT NOT NULL,
  logger          TEXT NOT NULL,
  body            TEXT NOT NULL,
  exception       TEXT,
  function        TEXT,
  file_path       TEXT,
  line            INTEGER,
  stored_bytes    INTEGER NOT NULL
);
CREATE INDEX ix_lr_ts ON log_records(ts_ns, seq);
CREATE INDEX ix_lr_received ON log_records(received_ns);
"""


def stderr_line(message: str) -> None:
    """Report a pipeline problem once, outside every captured logger."""
    try:
        sys.stderr.write(f"otel_logs: {message}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        return  # stderr is closed; there is nowhere left to report to


class StoreUnavailable(RuntimeError):
    """The store file is missing or unreadable."""


class ReadTimeout(RuntimeError):
    """A read hit its deadline."""


@dataclass
class StoreCounters:
    """Process-lifetime counts of every record not stored, and every prune."""

    inserted: int = 0
    dropped_store_cap: int = 0
    dropped_disk_floor: int = 0
    dropped_busy: int = 0
    dropped_store_error: int = 0
    pruned_age: int = 0
    pruned_size: int = 0

    def as_dict(self) -> dict[str, int]:
        """The counters as a plain mapping for ``log_read``'s capture status."""
        return dict(vars(self))


@dataclass
class LogStore:
    """The single writer for one store file."""

    path: Path
    max_bytes: int = DEFAULT_MAX_BYTES
    retention_seconds: int = RETENTION_SECONDS
    disk_floor_bytes: int = DISK_FLOOR_BYTES
    counters: StoreCounters = field(default_factory=StoreCounters)
    _db: sqlite3.Connection | None = None
    _lock_fd: int | None = None

    @property
    def soft_limit(self) -> int:
        """Inserts stop above this; r5's L = C − 64 MiB at the default C."""
        return self.max_bytes - self.max_bytes // 8

    @property
    def prune_target(self) -> int:
        """Size pruning stops once live data is under this."""
        return self.soft_limit - self.max_bytes // 16

    @property
    def wal_threshold(self) -> int:
        """A WAL above this is checkpointed before the next transaction."""
        return min(WAL_THRESHOLD_BYTES, self.max_bytes // 32)

    @property
    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("log store is not open")
        return self._db

    def open(self) -> bool:
        """Claim the writer lock and open the schema; False if another writer holds it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(f"{self.path}.writer.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self._lock_fd = fd
        try:
            try:
                self._db = self._connect_with_schema()
            except sqlite3.DatabaseError:
                # A corrupt file is evidence we can afford to lose.
                self._remove_files()
                self._db = self._connect_with_schema()
        except Exception:
            self.close()
            raise
        return True

    def _remove_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=0.25, isolation_level=None, check_same_thread=False)
        db.execute(f"PRAGMA page_size={PAGE_SIZE}")
        db.execute("PRAGMA auto_vacuum=INCREMENTAL")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("PRAGMA busy_timeout=250")
        db.execute(f"PRAGMA journal_size_limit={WAL_THRESHOLD_BYTES}")
        return db

    def _connect_with_schema(self) -> sqlite3.Connection:
        db = self._connect()
        try:
            row = db.execute(
                "SELECT value FROM store_meta WHERE key='schema_version'"
            ).fetchone()
        except sqlite3.DatabaseError:
            row = None
        if row is not None and row[0] == SCHEMA_VERSION:
            return db
        tables = db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
        if tables:
            # Wrong or unknown version: logs are evidence, recreate the file.
            db.close()
            self._remove_files()
            db = self._connect()
        db.executescript("BEGIN;" + _SCHEMA + "COMMIT;")
        db.execute(
            "INSERT INTO store_meta (key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        return db

    def close(self) -> None:
        """Checkpoint, close the connection and release the writer lock."""
        if self._db is not None:
            try:
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._db.close()
            self._db = None
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def footprint(self) -> int:
        """Summed on-disk size of the database, WAL and shared-memory files."""
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.stat(f"{self.path}{suffix}").st_size
            except FileNotFoundError:
                pass
        return total

    def _wal_bytes(self) -> int:
        try:
            return os.stat(f"{self.path}-wal").st_size
        except FileNotFoundError:
            return 0

    def _checkpoint_if_needed(self) -> None:
        if self._wal_bytes() > self.wal_threshold:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def _live_bytes(self) -> int:
        pages = self._conn.execute("PRAGMA page_count").fetchone()[0]
        free = self._conn.execute("PRAGMA freelist_count").fetchone()[0]
        return (pages - free) * PAGE_SIZE

    def insert(self, records: Sequence[CapturedRecord], received_ns: int) -> int:
        """Store one batch after the pre-write check; return rows stored."""
        if not records:
            return 0
        db = self._conn
        if shutil.disk_usage(self.path.parent).free < self.disk_floor_bytes:
            self.counters.dropped_disk_floor += len(records)
            return 0
        self._checkpoint_if_needed()
        if self.footprint() > self.soft_limit:
            self.prune(received_ns)
            if self.footprint() > self.soft_limit:
                self.counters.dropped_store_cap += len(records)
                return 0
        rows = [
            (
                r.ts_ns, received_ns, r.severity_number, r.severity_text, r.logger,
                r.body, r.exception, r.function, r.file_path, r.line, r.size,
            )
            for r in records
        ]
        try:
            db.execute("BEGIN IMMEDIATE")
            db.executemany(
                "INSERT INTO log_records (ts_ns, received_ns, severity_number, "
                "severity_text, logger, body, exception, function, file_path, "
                "line, stored_bytes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            db.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            self._rollback()
            if "locked" in str(exc) or "busy" in str(exc):
                self.counters.dropped_busy += len(records)
                return 0
            raise
        self.counters.inserted += len(rows)
        self._checkpoint_if_needed()
        return len(rows)

    def _rollback(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def _delete_chunk(self, where: str, params: tuple) -> int:
        db = self._conn
        db.execute("BEGIN IMMEDIATE")
        cursor = db.execute(
            "DELETE FROM log_records WHERE seq IN (SELECT seq FROM log_records "
            f"WHERE {where} ORDER BY received_ns, seq LIMIT ?)",
            (*params, PRUNE_CHUNK_ROWS),
        )
        db.execute("COMMIT")
        self._checkpoint_if_needed()
        return cursor.rowcount

    def prune(self, now_ns: int) -> None:
        """Delete expired rows, then the oldest rows until under the target; return space."""
        db = self._conn
        cutoff = now_ns - self.retention_seconds * 1_000_000_000
        while deleted := self._delete_chunk("received_ns < ?", (cutoff,)):
            self.counters.pruned_age += deleted
        while self._live_bytes() > self.prune_target:
            deleted = self._delete_chunk("1", ())
            if not deleted:
                break
            self.counters.pruned_size += deleted
        while db.execute("PRAGMA freelist_count").fetchone()[0]:
            db.execute("PRAGMA incremental_vacuum(256)")
            self._checkpoint_if_needed()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


@dataclass(frozen=True)
class ReadQuery:
    """Filters for one newest-first read."""

    since_ns: int
    until_ns: int
    min_severity: int
    logger_prefix: str | None = None
    contains: str | None = None
    limit: int = 30


def read_records(path: Path, query: ReadQuery) -> list[dict]:
    """Return up to ``query.limit + 1`` matching rows, newest first.

    Raises:
        StoreUnavailable: the file does not exist or cannot be opened.
        ReadTimeout: the query passed its deadline.
    """
    if not path.exists():
        raise StoreUnavailable("no log store on this instance")
    clauses = ["ts_ns >= ?", "ts_ns < ?", "severity_number >= ?"]
    params: list[object] = [query.since_ns, query.until_ns, query.min_severity]
    if query.logger_prefix:
        clauses.append("substr(logger, 1, ?) = ?")
        params += [len(query.logger_prefix), query.logger_prefix]
    if query.contains:
        needle = query.contains.lower()
        clauses.append(
            "(instr(lower(body), ?) > 0 OR instr(lower(coalesce(exception, '')), ?) > 0)"
        )
        params += [needle, needle]
    params.append(query.limit + 1)
    sql = (
        "SELECT ts_ns, severity_text, logger, body, exception, function, "
        "file_path, line FROM log_records WHERE " + " AND ".join(clauses)
        + " ORDER BY ts_ns DESC, seq DESC LIMIT ?"
    )
    deadline = time.monotonic() + READ_DEADLINE_SECONDS
    try:
        with closing(
            sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.25)
        ) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA busy_timeout=250")
            db.set_progress_handler(lambda: 1 if time.monotonic() >= deadline else 0, 1000)
            return [dict(row) for row in db.execute(sql, params).fetchall()]
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc).lower() or time.monotonic() >= deadline:
            raise ReadTimeout("log read passed its deadline") from exc
        raise StoreUnavailable(str(exc)) from exc
    except sqlite3.DatabaseError as exc:
        raise StoreUnavailable(str(exc)) from exc
