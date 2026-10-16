"""Snapshot and restore an adopted state root around the app's first start."""

from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


def create_adoption_snapshot(state_root: Path) -> Path:
    """Snapshot an adopted state root before a newer app can migrate it.

    The snapshot deliberately lives under ``backups/adopt-*``: database
    migration retention only examines sibling pre-migration files and cannot
    prune this transaction's recovery point. SQLite owns the database copy;
    every other state entry except older backups is copied with its mode.
    """
    state_root = Path(state_root).resolve()
    database = state_root / "corsia.db"
    if not database.is_file():
        raise FileNotFoundError(f"missing CORSIA database at {database}")
    snapshot = state_root / "backups" / (
        f"adopt-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex}"
    )
    snapshot.mkdir(parents=True, mode=0o700)
    snapshot.chmod(0o700)
    database_copy = snapshot / "corsia.db"
    with sqlite3.connect(database) as source, sqlite3.connect(database_copy) as destination:
        source.backup(destination)
    database_copy.chmod(database.stat().st_mode & 0o777)

    files = snapshot / "files"
    files.mkdir(mode=0o700)
    files.chmod(0o700)
    for source in state_root.iterdir():
        if source.name in {"backups", "corsia.db", "corsia.db-wal", "corsia.db-shm"}:
            continue
        destination = files / source.name
        if source.is_dir():
            shutil.copytree(source, destination, copy_function=shutil.copy2)
        else:
            shutil.copy2(source, destination)
    return snapshot


def restore_adoption_snapshot(state_root: Path, snapshot: Path) -> None:
    """Restore the full state-root snapshot after the app service is stopped."""
    state_root = Path(state_root).resolve()
    snapshot = Path(snapshot).resolve()
    database_copy = snapshot / "corsia.db"
    files = snapshot / "files"
    if not database_copy.is_file() or not files.is_dir():
        raise ValueError("adoption snapshot is incomplete")
    for name in ("corsia.db-wal", "corsia.db-shm"):
        (state_root / name).unlink(missing_ok=True)
    for entry in state_root.iterdir():
        if entry.name == "backups":
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    restoring = state_root / ".corsia.db.adoption-restore"
    restoring.unlink(missing_ok=True)
    with sqlite3.connect(database_copy) as source, sqlite3.connect(restoring) as destination:
        source.backup(destination)
    restoring.chmod(database_copy.stat().st_mode & 0o777)
    os.replace(restoring, state_root / "corsia.db")
    for source in files.iterdir():
        destination = state_root / source.name
        if source.is_dir():
            shutil.copytree(source, destination, copy_function=shutil.copy2)
        else:
            shutil.copy2(source, destination)
