"""Reap leaked CORSIA SQLite snapshots from idle temporary worker trays.

This is deliberately narrower than lifecycle retention: only recognized,
validated database copies beneath a temporary worker's ``working`` or
``scratch`` tray can be removed. Every uncertainty keeps the file.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
import os
from pathlib import Path
import sqlite3
import stat
import time
from typing import Iterator

from loguru import logger

from infra import config
from infra.database import get_db


MINIMUM_BYTES = 1024 ** 3
IDLE_SECONDS = 24 * 60 * 60
DATABASE_NAMES = {"corsia.db", "corsia.db-wal", "corsia.db-shm"}


@dataclass(frozen=True)
class ThreadState:
    """The small, authoritative state slice needed for a cleanup decision."""

    is_temporary: bool
    is_active: bool
    updated_at: float | None


@dataclass(frozen=True)
class Candidate:
    """A regular, named candidate whose lstat details can be rechecked."""

    path: Path
    size: int
    mtime: float
    inode: int


@dataclass
class ReapReport:
    """Counts only: cleanup logs never disclose paths or thread identities."""

    files_reaped: int = 0
    bytes_freed: int = 0
    skipped: Counter[str] = field(default_factory=Counter)

    def skip(self, reason: str) -> None:
        self.skipped[reason] += 1


def _parse_timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _thread_states(db) -> dict[str, ThreadState]:
    """Read temporary-worker, activity, and active-turn state in two queries."""
    active = {
        row["thread_id"]
        for row in db.execute(
            "SELECT DISTINCT thread_id FROM active_turns "
            "WHERE status IN ('running', 'waiting_retry')"
        )
    }
    rows = db.execute(
        "SELECT id, spawned_by, agent_enabled, updated_at FROM threads"
    ).fetchall()
    return {
        row["id"]: ThreadState(
            is_temporary=row["spawned_by"] is not None
            and not bool(row["agent_enabled"]),
            is_active=row["id"] in active,
            updated_at=_parse_timestamp(row["updated_at"]),
        )
        for row in rows
    }


def _is_snapshot_name(name: str) -> bool:
    prefix = "corsia.db.pre-migration-"
    return name in DATABASE_NAMES or (
        name.startswith(prefix)
        and name.endswith(".bak")
        and len(name) > len(prefix) + len(".bak")
    )


def _candidate(path: Path, *, now: float, minimum_bytes: int, report: ReapReport) -> Candidate | None:
    try:
        details = path.lstat()
    except OSError:
        report.skip("unreadable")
        return None
    if stat.S_ISLNK(details.st_mode):
        report.skip("symlink")
        return None
    if not stat.S_ISREG(details.st_mode):
        report.skip("not_regular")
        return None
    if details.st_size < minimum_bytes:
        report.skip("too_small")
        return None
    if now - details.st_mtime < IDLE_SECONDS:
        report.skip("recent_file")
        return None
    return Candidate(path, details.st_size, details.st_mtime, details.st_ino)


def _walk_tray(tray: Path) -> Iterator[Path]:
    """Yield named entries recursively without following any directory symlink."""
    try:
        with os.scandir(tray) as entries:
            for entry in entries:
                path = Path(entry.path)
                if entry.is_symlink():
                    if _is_snapshot_name(entry.name):
                        yield path
                    continue
                if entry.is_dir(follow_symlinks=False):
                    yield from _walk_tray(path)
                elif _is_snapshot_name(entry.name):
                    yield path
    except OSError:
        return


def _project_trays(workspace: Path) -> Iterator[tuple[str, Path]]:
    """Yield each project temporary tray, ignoring malformed layout safely."""
    projects = workspace / "projects"
    try:
        with os.scandir(projects) as project_entries:
            for project in project_entries:
                if project.is_symlink() or not project.is_dir(follow_symlinks=False):
                    continue
                threads = Path(project.path) / ".threads"
                try:
                    with os.scandir(threads) as thread_entries:
                        for thread in thread_entries:
                            if thread.is_symlink() or not thread.is_dir(follow_symlinks=False):
                                continue
                            for tray_name in ("working", "scratch"):
                                tray = Path(thread.path) / tray_name
                                try:
                                    if tray.is_symlink() or not tray.is_dir():
                                        continue
                                except OSError:
                                    continue
                                yield thread.name, tray
                except OSError:
                    continue
    except OSError:
        return


def _is_corsia_database(candidate: Candidate) -> bool:
    """Prove the file is a readable CORSIA SQLite database before unlinking it."""
    try:
        with candidate.path.open("rb") as source:
            if source.read(16) != b"SQLite format 3\x00":
                return False
        uri = f"{candidate.path.as_uri()}?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True) as db:
            user_version = db.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('threads', 'messages')"
                )
            }
        return user_version > 0 and tables == {"threads", "messages"}
    except (OSError, sqlite3.Error, ValueError):
        return False


def _unchanged(candidate: Candidate) -> bool:
    try:
        details = candidate.path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(details.st_mode)
        and not stat.S_ISLNK(details.st_mode)
        and details.st_ino == candidate.inode
        and details.st_size == candidate.size
        and details.st_mtime == candidate.mtime
    )


def _reap(candidate: Candidate, report: ReapReport) -> bool:
    if not _unchanged(candidate):
        report.skip("changed")
        return False
    try:
        candidate.path.unlink()
    except OSError:
        report.skip("unlink_failed")
        return False
    report.files_reaped += 1
    report.bytes_freed += candidate.size
    return True


def _thread_skip_reason(state: ThreadState | None, now: float) -> str | None:
    if state is None:
        return "unknown_thread"
    if not state.is_temporary:
        return "not_temporary"
    if state.is_active:
        return "active_turn"
    if state.updated_at is None:
        return "unknown_activity"
    if now - state.updated_at < IDLE_SECONDS:
        return "recent_activity"
    return None


def _reap_database_bundle(
    database: Candidate,
    *,
    now: float,
    minimum_bytes: int,
    report: ReapReport,
) -> None:
    if not _is_corsia_database(database):
        report.skip("not_corsia_database")
        return
    companions = [
        candidate
        for suffix in ("-wal", "-shm")
        if (candidate := _candidate(
            database.path.with_name(database.path.name + suffix),
            now=now, minimum_bytes=minimum_bytes, report=report,
        )) is not None
    ]
    if not _reap(database, report):
        return
    for companion in companions:
        _reap(companion, report)


def reap_snapshot_copies(
    workspace: Path,
    db,
    *,
    now: float | None = None,
    minimum_bytes: int = MINIMUM_BYTES,
) -> ReapReport:
    """Reap only validated old snapshot copies from idle temporary worker trays."""
    timestamp = time.time() if now is None else now
    report = ReapReport()
    try:
        states = _thread_states(db)
    except Exception:
        report.skip("state_unavailable")
        logger.info(
            "snapshot_reaper: files_reaped={} bytes_freed={} skipped={}",
            report.files_reaped, report.bytes_freed, dict(sorted(report.skipped.items())),
        )
        return report
    for thread_id, tray in _project_trays(workspace):
        state_reason = _thread_skip_reason(states.get(thread_id), timestamp)
        for path in _walk_tray(tray):
            if path.name in {"corsia.db-wal", "corsia.db-shm"}:
                continue
            candidate = _candidate(
                path, now=timestamp, minimum_bytes=minimum_bytes, report=report,
            )
            if candidate is None:
                continue
            if state_reason is not None:
                report.skip(state_reason)
                continue
            if candidate.path.name == "corsia.db":
                _reap_database_bundle(
                    candidate, now=timestamp, minimum_bytes=minimum_bytes, report=report,
                )
            elif _is_corsia_database(candidate):
                _reap(candidate, report)
            else:
                report.skip("not_corsia_database")
    logger.info(
        "snapshot_reaper: files_reaped={} bytes_freed={} skipped={}",
        report.files_reaped, report.bytes_freed, dict(sorted(report.skipped.items())),
    )
    return report


def reap_snapshot_copies_nightly() -> ReapReport:
    """Run the workspace snapshot cleanup from the nightly maintenance job."""
    db = get_db()
    try:
        return reap_snapshot_copies(config.WORKSPACE, db)
    finally:
        db.close()
