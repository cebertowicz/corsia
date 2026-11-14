"""How a document live store's connections open, and its journal mode."""
from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from pathlib import Path

from infra.database import is_lock_refusal

log = logging.getLogger(__name__)

# Stores that stayed in rollback-journal mode in this process. The integrity check reads the
# whole file, so it runs at most once per store per process; a busy store converts on the
# first open after a restart.
_stayed: set[Path] = set()


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=1.0, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=1000")
        connection.execute("PRAGMA synchronous=FULL")
    except BaseException:
        # Closed now, not whenever garbage collection reaches it: see probe_read_only.
        connection.close()
        raise
    return connection


def probe_read_only(path: Path) -> None:
    """Read an existing store's header and schema before any writable connection opens.

    A writable connection that is the last to close checkpoints the -wal into the
    main file and deletes it, even after refusing a damaged file. A read-only
    connection cannot, so a store refused here keeps its main file and -wal exactly
    as found: they may hold acknowledged typing.

    A rollback-journal store left with a hot -journal by a crash mid-transaction
    is not damaged: only a writable connection can roll it back, so that refusal
    passes and the writable open recovers it as it always has.
    """
    try:
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1.0)) as db:
            db.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.Error as exc:
        if getattr(exc, "sqlite_errorcode", None) != sqlite3.SQLITE_READONLY_ROLLBACK:
            raise


def enter_wal(connection: sqlite3.Connection, path: Path) -> None:
    """Move a healthy rollback-journal store to WAL, once; the mode is kept in the file.

    WAL keeps a commit in flight from blocking other connections and reads. Changing the
    mode rewrites header bytes, so a store that fails its integrity check is left exactly
    as found: a refused store's bytes may hold acknowledged typing. Either way the open
    carries on and says why it stayed in rollback-journal mode, once per store per process.
    """
    if connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
        return
    if path in _stayed:
        return
    step = "quick_check"
    try:
        check = connection.execute("PRAGMA quick_check").fetchone()
        if check is None or check[0] != "ok":
            _stays(path, "quick_check failed", "no result" if check is None else check[0])
            return
        step = "mode change"
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]  # SQLite answers with the mode it is in
        if mode != "wal":
            _stays(path, "SQLite did not enter WAL", f"still in {mode} mode")
    except sqlite3.OperationalError as exc:
        if is_lock_refusal(exc):
            _stays(path, "busy, will convert on the first open after a restart", exc)
        else:
            _stays(path, f"{step} failed", exc)
    except sqlite3.DatabaseError as exc:  # damage is the reads' to report, not this open's
        _stays(path, f"{step} failed", exc)


def _stays(path: Path, why: str, detail: object) -> None:
    _stayed.add(path)
    log.warning("Live store %s stays in rollback-journal mode: %s (%s)", path, why, detail)
