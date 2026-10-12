"""Snapshot cleanup only touches validated old copies from idle temporary workers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3

import pytest

from infra.snapshot_reaper import reap_snapshot_copies


NOW = datetime(2026, 10, 12, tzinfo=timezone.utc).timestamp()
OLD = datetime(2026, 10, 10, tzinfo=timezone.utc).isoformat()
RECENT = datetime(2026, 10, 12, tzinfo=timezone.utc).isoformat()


@pytest.fixture
def state_db(tmp_path):
    db = sqlite3.connect(tmp_path / "state.db")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, spawned_by TEXT, "
        "agent_enabled INTEGER, updated_at TEXT)"
    )
    db.execute("CREATE TABLE active_turns (thread_id TEXT, status TEXT)")
    yield db
    db.close()


def _thread(db, thread_id: str, *, updated_at: str = OLD, active: bool = False,
            persistent: bool = False) -> None:
    db.execute(
        "INSERT INTO threads VALUES (?, ?, ?, ?)",
        (thread_id, "parent", int(persistent), updated_at),
    )
    if active:
        db.execute("INSERT INTO active_turns VALUES (?, 'running')", (thread_id,))
    db.commit()


def _tray(workspace: Path, thread_id: str, name: str = "working") -> Path:
    tray = workspace / "projects" / "project" / ".threads" / thread_id / name
    tray.mkdir(parents=True)
    return tray


def _corsia_database(path: Path, *, valid: bool = True) -> None:
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 1")
        if valid:
            db.execute("CREATE TABLE threads (id TEXT)")
            db.execute("CREATE TABLE messages (id TEXT)")
        else:
            db.execute("CREATE TABLE unrelated (id TEXT)")
    old = NOW - 2 * 86400
    os.utime(path, (old, old))


def _old_file(path: Path) -> None:
    path.write_bytes(b"x")
    old = NOW - 2 * 86400
    os.utime(path, (old, old))


def _run(workspace: Path, db, *, minimum_bytes: int = 1):
    return reap_snapshot_copies(workspace, db, now=NOW, minimum_bytes=minimum_bytes)


def test_reaps_idle_temporary_database_and_its_qualified_sidecars(tmp_path, state_db):
    _thread(state_db, "idle")
    tray = _tray(tmp_path, "idle") / "nested"
    tray.mkdir()
    database = tray / "corsia.db"
    _corsia_database(database)
    wal = tray / "corsia.db-wal"
    shm = tray / "corsia.db-shm"
    _old_file(wal)
    _old_file(shm)

    report = _run(tmp_path, state_db)

    assert report.files_reaped == 3
    assert report.bytes_freed > 0
    assert not database.exists()
    assert not wal.exists()
    assert not shm.exists()


@pytest.mark.parametrize(
    ("thread_kwargs", "expected_reason"),
    [
        ({"active": True}, "active_turn"),
        ({"updated_at": RECENT}, "recent_activity"),
        ({"persistent": True}, "not_temporary"),
    ],
)
def test_preserves_non_idle_or_persistent_worker_copy(
    tmp_path, state_db, thread_kwargs, expected_reason,
):
    _thread(state_db, "worker", **thread_kwargs)
    database = _tray(tmp_path, "worker") / "corsia.db"
    _corsia_database(database)

    report = _run(tmp_path, state_db)

    assert database.exists()
    assert report.files_reaped == 0
    assert report.skipped == {expected_reason: 1}


def test_preserves_non_corsia_sqlite_and_wrong_name(tmp_path, state_db):
    _thread(state_db, "idle")
    tray = _tray(tmp_path, "idle", "scratch")
    unrelated = tray / "corsia.db.pre-migration-v1.bak"
    _corsia_database(unrelated, valid=False)
    wrong_name = tray / "other.db"
    _old_file(wrong_name)

    report = _run(tmp_path, state_db)

    assert unrelated.exists()
    assert wrong_name.exists()
    assert report.files_reaped == 0
    assert report.skipped == {"not_corsia_database": 1}


def test_preserves_symlink_without_following_it(tmp_path, state_db):
    _thread(state_db, "idle")
    outside = tmp_path / "outside.db"
    _corsia_database(outside)
    link = _tray(tmp_path, "idle") / "corsia.db"
    link.symlink_to(outside)

    report = _run(tmp_path, state_db)

    assert link.is_symlink()
    assert outside.exists()
    assert report.files_reaped == 0
    assert report.skipped == {"symlink": 1}


def test_never_reaps_sidecars_without_reaping_the_database(tmp_path, state_db):
    _thread(state_db, "idle")
    tray = _tray(tmp_path, "idle")
    wal = tray / "corsia.db-wal"
    shm = tray / "corsia.db-shm"
    _old_file(wal)
    _old_file(shm)

    report = _run(tmp_path, state_db)

    assert wal.exists()
    assert shm.exists()
    assert report.files_reaped == 0
    assert report.skipped == {}


def test_run_log_contains_counts_but_not_paths(tmp_path, state_db, monkeypatch):
    _thread(state_db, "idle")
    database = _tray(tmp_path, "idle") / "corsia.db"
    _corsia_database(database)
    messages: list[tuple[object, ...]] = []

    from infra import snapshot_reaper

    monkeypatch.setattr(snapshot_reaper.logger, "info", lambda *args: messages.append(args))
    _run(tmp_path, state_db)

    assert len(messages) == 1
    assert "files_reaped" in str(messages[0][0])
    assert str(database) not in str(messages[0])
    assert "idle" not in str(messages[0])
