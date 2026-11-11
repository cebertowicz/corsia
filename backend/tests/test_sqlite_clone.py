"""The pre-migration backup's clone path: a consistent, self-contained snapshot, or a clean fallback."""

from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import pytest

import infra.sqlite_clone as sqlite_clone
from infra.database_migrations import MIGRATIONS, migrate_to_current
from infra.sqlite_clone import CloneUnsupported, clone_database


@pytest.fixture
def ladder():
    """Register migration steps for one test and restore the real ladder."""
    original = dict(MIGRATIONS)
    MIGRATIONS.clear()
    yield MIGRATIONS
    MIGRATIONS.clear()
    MIGRATIONS.update(original)


def _wal_database(path: Path, version: int) -> sqlite3.Connection:
    """A WAL database whose last commits are still only in its WAL, as on a live host."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, label TEXT)")
    conn.execute(f"PRAGMA user_version={version}")
    conn.executemany("INSERT INTO t (label) VALUES (?)", [(f"row {i}",) for i in range(500)])
    assert Path(f"{path}-wal").stat().st_size > 0
    return conn


def _supports_clone(tmp_path: Path) -> bool:
    probe = tmp_path / "probe"
    probe.write_bytes(b"x")
    try:
        sqlite_clone._clone_file(probe, tmp_path / "probe-clone")
    except CloneUnsupported:
        return False
    return True


def test_a_clone_under_the_write_lock_is_a_self_contained_snapshot(tmp_path):
    if not _supports_clone(tmp_path):
        pytest.skip("this file system cannot clone; the fallback test covers it")
    source = tmp_path / "live.db"
    writer = _wal_database(source, 7)
    holder = sqlite3.connect(source, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    target = tmp_path / "live.db.bak"
    try:
        clone_database(source, target, expected_user_version=7)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        writer.close()

    assert not Path(f"{target}-wal").exists()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    backup = sqlite3.connect(target)
    try:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 7
        assert backup.execute("SELECT count(*) FROM t").fetchone()[0] == 500
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        backup.close()


def test_a_passive_checkpoint_between_the_two_clones_does_not_change_the_snapshot(tmp_path, monkeypatch):
    """Another connection may checkpoint while the lock is held; the cloned WAL still replays those pages."""
    if not _supports_clone(tmp_path):
        pytest.skip("this file system cannot clone")
    source = tmp_path / "live.db"
    writer = _wal_database(source, 7)
    writer.execute("UPDATE t SET label = 'final ' || id")
    checkpointer = sqlite3.connect(source, isolation_level=None)
    real_clone = sqlite_clone._clone_file
    moved: list[int] = []

    def clone_then_checkpoint(src, dst):
        real_clone(src, dst)
        if not moved:
            _busy, _frames, backfilled = checkpointer.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            moved.append(backfilled)

    monkeypatch.setattr(sqlite_clone, "_clone_file", clone_then_checkpoint)
    holder = sqlite3.connect(source, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    target = tmp_path / "live.db.bak"
    try:
        clone_database(source, target, expected_user_version=7)
    finally:
        holder.execute("ROLLBACK")
        for conn in (holder, checkpointer, writer):
            conn.close()

    assert moved and moved[0] > 0, "the checkpoint must have written WAL pages into the live file"
    backup = sqlite3.connect(target)
    try:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT count(*) FROM t WHERE label LIKE 'final %'").fetchone()[0] == 500
    finally:
        backup.close()


def test_a_clone_reading_the_wrong_version_leaves_nothing_behind(tmp_path):
    if not _supports_clone(tmp_path):
        pytest.skip("this file system cannot clone")
    source = tmp_path / "live.db"
    _wal_database(source, 7).close()
    target = tmp_path / "live.db.bak"

    with pytest.raises(RuntimeError, match="user_version 7"):
        clone_database(source, target, expected_user_version=8)

    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("live.db.bak")) == []


def test_without_a_clone_the_migration_still_backs_up_by_copying_pages(tmp_path, ladder, monkeypatch):
    def refuse(_source, _target):
        raise CloneUnsupported("test file system")

    monkeypatch.setattr(sqlite_clone, "_clone_file", refuse)
    ladder[3] = lambda conn: conn.execute("CREATE TABLE migrated (x)")
    source = tmp_path / "live.db"
    writer = _wal_database(source, 3)
    writer.close()

    conn = sqlite3.connect(source)
    try:
        result = migrate_to_current(conn, 4)
    finally:
        conn.close()

    backup = sqlite3.connect(result.backup_path)
    try:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 3
        assert backup.execute("SELECT count(*) FROM t").fetchone()[0] == 500
        assert backup.execute("SELECT count(*) FROM sqlite_master WHERE name='migrated'").fetchone()[0] == 0
    finally:
        backup.close()


def test_a_clone_backup_skips_the_copy_room_check_and_its_pruning(tmp_path, ladder, monkeypatch):
    """The copy's room check credits each backup's full size, which a clone does not hold."""
    if not _supports_clone(tmp_path):
        pytest.skip("this file system cannot clone")
    import infra.database_migrations as migrations

    def copy_room_check(*_args):
        raise AssertionError("a clone must not prune backups to make room for a full copy")

    monkeypatch.setattr(migrations, "_ensure_backup_room", copy_room_check)
    ladder[3] = lambda conn: conn.execute("CREATE TABLE migrated (x)")
    source = tmp_path / "live.db"
    _wal_database(source, 3).close()

    conn = sqlite3.connect(source)
    try:
        assert migrate_to_current(conn, 4).backup_path is not None
    finally:
        conn.close()


def test_without_room_for_the_reserve_the_copy_room_check_decides(tmp_path, ladder, monkeypatch):
    import infra.database_migrations as migrations

    monkeypatch.setattr(migrations, "_BACKUP_FREE_FLOOR_BYTES", 10**18)
    ladder[3] = lambda conn: conn.execute("CREATE TABLE migrated (x)")
    source = tmp_path / "live.db"
    _wal_database(source, 3).close()

    conn = sqlite3.connect(source)
    try:
        with pytest.raises(migrations.MigrationError, match="not enough free space"):
            migrate_to_current(conn, 4)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    finally:
        conn.close()
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".bak")]


def test_a_wal_database_migrates_with_its_unfolded_commits_in_the_backup(tmp_path, ladder):
    """The end-to-end path a live host takes: last commits only in the WAL, then a migration."""
    ladder[3] = lambda conn: conn.execute("CREATE TABLE migrated (x)")
    source = tmp_path / "live.db"
    writer = _wal_database(source, 3)

    conn = sqlite3.connect(source)
    try:
        result = migrate_to_current(conn, 4)
    finally:
        conn.close()
        writer.close()

    backup = sqlite3.connect(result.backup_path)
    try:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 3
        assert backup.execute("SELECT count(*) FROM t").fetchone()[0] == 500
        assert backup.execute("SELECT count(*) FROM sqlite_master WHERE name='migrated'").fetchone()[0] == 0
    finally:
        backup.close()
