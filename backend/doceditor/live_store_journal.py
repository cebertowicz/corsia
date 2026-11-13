"""Journal mode of a document live store."""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)


def enter_wal(connection: sqlite3.Connection, path: Path) -> None:
    """Move a healthy rollback-journal store to WAL, once; the mode is kept in the file.

    WAL keeps a commit in flight from blocking other connections and reads. Changing the
    mode rewrites header bytes, so a store that fails its integrity check is left exactly
    as found: a refused store's bytes may hold acknowledged typing. Either way the open
    carries on and says why it stayed in rollback-journal mode, once per open.
    """
    if connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
        return
    try:
        check = connection.execute("PRAGMA quick_check").fetchone()
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc) or "busy" in str(exc):
            log.warning("Live store %s stays in rollback-journal mode: busy, will convert on next open (%s)", path, exc)
        else:
            log.warning("Live store %s stays in rollback-journal mode: quick_check failed (%s)", path, exc)
        return
    except sqlite3.DatabaseError as exc:  # damage is the reads' to report, not this open's
        log.warning("Live store %s stays in rollback-journal mode: quick_check failed (%s)", path, exc)
        return
    if check is None or check[0] != "ok":
        log.warning("Live store %s stays in rollback-journal mode: quick_check failed (%s)",
                    path, "no result" if check is None else check[0])
        return
    connection.execute("PRAGMA journal_mode=WAL")
