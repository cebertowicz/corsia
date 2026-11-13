"""Keep checkpoint I/O outside the main database's writer-admission ticket."""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from infra import startup_progress

log = logging.getLogger(__name__)
_WAL_FRAMES = 1000  # SQLite's existing default autocheckpoint budget


@dataclass(frozen=True)
class WalCheckpoint:
    wal_path: Path
    threshold_bytes: int

    @classmethod
    def configure(cls, connection: sqlite3.Connection, path: str) -> WalCheckpoint:
        """Defer the pooled connection's checkpoint until after admission releases."""
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        threshold = 32 + _WAL_FRAMES * (page_size + 24)
        connection.execute("PRAGMA wal_autocheckpoint=0")
        # PASSIVE retains the WAL allocation. On its next reset SQLite trims it
        # to this budget, so an old high-water file cannot defeat the size gate
        # and make every subsequent small commit checkpoint again.
        connection.execute(f"PRAGMA journal_size_limit={threshold}")
        return cls(Path(path + "-wal"), threshold)

    def run(self, connection: sqlite3.Connection) -> None:
        """Checkpoint once on growth; pinned readers keep their original snapshot."""
        try:
            if self.wal_path.stat().st_size > self.threshold_bytes:
                # PASSIVE neither waits for readers/writers nor takes the WAL
                # writer lock. Another checkpointer returns busy immediately.
                with startup_progress.transaction_writes(connection):
                    connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        except FileNotFoundError:
            pass  # no WAL, including an in-memory database or a completed close
        except (OSError, sqlite3.Error):
            # The write is already committed. Maintenance cannot make that
            # durable write report failure or suppress its committed effects.
            log.warning("SQLite post-commit checkpoint failed", exc_info=True)


def checkpoint_after_write(connection: sqlite3.Connection) -> None:
    """Only main pooled connections opt in; foreign/readonly handles stay unchanged."""
    checkpoint = getattr(connection, "wal_checkpoint", None)
    if checkpoint is not None and not connection.in_transaction:
        checkpoint.run(connection)
