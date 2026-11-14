"""Journal mode of a document live store."""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from infra.database import is_lock_refusal

log = logging.getLogger(__name__)

# Stores that stayed in rollback-journal mode in this process. The integrity check reads the
# whole file, so it runs at most once per store per process; a busy store converts on the
# first open after a restart.
_stayed: set[Path] = set()


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
