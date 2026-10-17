"""Snapshot and restore an adopted state root around the app's first start."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from infra.database_migrations import backup_room
from infra.ignored_environment import IGNORED_VARIABLES

#: Exit status of :func:`preflight_main` when adoption is refused with a message
#: that is safe to show; any other failure is not forwarded.
REFUSED = 3


class AdoptionRefused(Exception):
    """Adoption cannot start. The text names keys and byte counts, never a value."""


def _copy_entry(source: Path, destination: Path) -> None:
    """Copy one state entry as it is: a link stays a link, so a restore puts back the exact links."""
    if source.is_symlink():
        os.symlink(os.readlink(source), destination)
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True, copy_function=shutil.copy2)
    else:
        shutil.copy2(source, destination)


def stored_key_names(state_root: Path) -> set[str]:
    """Names of the key store entries that hold a value. Names only: no value is returned or logged."""
    state_root = Path(state_root)
    database = state_root / "corsia.db"
    try:
        with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
            rows = connection.execute(
                "SELECT name, value_ref FROM key_store_entries WHERE value_ref IS NOT NULL"
            ).fetchall()
    except sqlite3.OperationalError as error:
        if "no such table" not in str(error):
            raise AdoptionRefused("The key store could not be read.") from None
        return set()  # no key store table yet: nothing is stored
    try:
        values = json.loads((state_root / "keys" / "values.json").read_bytes())
    except FileNotFoundError:
        return set()
    except ValueError:
        raise AdoptionRefused("The key store file could not be read.") from None
    if not isinstance(values, dict):
        raise AdoptionRefused("The key store file could not be read.")
    return {name for name, ref in rows if isinstance(values.get(ref), str) and values[ref]}


def check_adoption_preflight(state_root: Path, variables: list[str]) -> None:
    """Refuse, before the old service drains, what would fail after it has stopped.

    Every key variable CORSIA ever imported from the environment must already be
    stored under its entry; the missing ones are named. The disk must hold the
    snapshot (the state root's size outside ``backups/``) and keep its reserve.
    """
    state_root = Path(state_root).resolve()
    present = set(variables)
    wanted = {variable: entry for entry, variable in IGNORED_VARIABLES.items() if variable in present}
    stored = stored_key_names(state_root)
    missing = sorted(variable for variable, entry in wanted.items() if entry not in stored)
    if missing:
        raise AdoptionRefused(
            "These provider keys aren’t stored in CORSIA yet: "
            f"{', '.join(missing)}. Add them in CORSIA › Settings › Keys, then choose Try again."
        )
    needed = sum(
        path.stat().st_size
        for top in state_root.iterdir()
        if top.name != "backups"
        for path in ([top] if top.is_file() else top.rglob("*"))
        if path.is_file() and not path.is_symlink()
    )
    # The snapshot takes ``needed``; the packaged backend's own pre-migration
    # backup then needs the shared room calculation on top, so both must fit
    # before the old service is stopped.
    required, available = backup_room(state_root / "corsia.db")
    # ``available`` counts old pre-migration backups the migration prunes later, so
    # the snapshot itself must also fit in what is free now.
    if available < needed + required or shutil.disk_usage(state_root).free < needed:
        raise AdoptionRefused(
            f"The backup needs {(needed + required) / 1e9:.1f} GB free and this disk has "
            f"{available / 1e9:.1f} GB. Free up space, then choose Try again."
        )


def preflight_main(arguments: list[str]) -> int:
    """Entry for the app: ``[state_root, *key_variable_names]``. Exit 0 ready, 3 refused."""
    try:
        check_adoption_preflight(Path(arguments[0]), arguments[1:])
    except AdoptionRefused as refusal:
        print(refusal, file=sys.stderr)
        return REFUSED
    return 0


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
    try:
        snapshot.chmod(0o700)
        database_copy = snapshot / "corsia.db"
        with closing(sqlite3.connect(database)) as live, closing(sqlite3.connect(database_copy)) as copy:
            live.backup(copy)
        database_copy.chmod(database.stat().st_mode & 0o777)

        files = snapshot / "files"
        files.mkdir(mode=0o700)
        files.chmod(0o700)
        for entry in state_root.iterdir():
            if entry.name in {"backups", "corsia.db", "corsia.db-wal", "corsia.db-shm"}:
                continue
            _copy_entry(entry, files / entry.name)
    except BaseException:
        # Retention skips backups/, so a partial copy would sit there for good
        # and hold the space the restarted old service needs.
        shutil.rmtree(snapshot, ignore_errors=True)
        raise
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
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    restoring = state_root / ".corsia.db.adoption-restore"
    restoring.unlink(missing_ok=True)
    with closing(sqlite3.connect(database_copy)) as saved, closing(sqlite3.connect(restoring)) as target:
        saved.backup(target)
    restoring.chmod(database_copy.stat().st_mode & 0o777)
    os.replace(restoring, state_root / "corsia.db")
    for entry in files.iterdir():
        _copy_entry(entry, state_root / entry.name)
