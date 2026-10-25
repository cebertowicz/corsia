"""Repoint the configuration values that store an absolute state path.

The desktop moves the whole state folder in one rename, so these values still
name the old place on the first start after the move. Each is re-derived from
the current ``STATE_ROOT``; history (messages, logs) is never rewritten. Every
step is idempotent and does nothing when the paths already agree. Managed oMLX
needs nothing here: every folder it uses is passed at launch from the current
state root (``omlx_supervisor_config.managed_launch_settings``).
"""

from __future__ import annotations

import logging
from pathlib import Path

from infra.database import get_db, write_transaction

log = logging.getLogger("infra.moved_state_heal")

_LIVE_HANDLE_STATES = ("running", "death_unverified")


def heal_moved_state_paths() -> None:
    """Rebase the log paths of process handles that are still live."""
    from infra.config import RUNTIME_DIR

    directory = Path(RUNTIME_DIR) / "managed-processes"
    rows = get_db().execute(
        "SELECT handle, log_path FROM managed_process_handles WHERE state IN (?, ?)",
        _LIVE_HANDLE_STATES,
    ).fetchall()
    moved = [
        (str(directory / Path(row["log_path"]).name), row["handle"])
        for row in rows
        if row["log_path"] and Path(row["log_path"]).parent != directory
    ]
    if not moved:
        return
    with write_transaction(None, scope="heal_process_log_paths") as connection:
        connection.executemany(
            "UPDATE managed_process_handles SET log_path = ? WHERE handle = ?", moved,
        )
    log.info("moved state: repointed %d running process log path(s)", len(moved))
