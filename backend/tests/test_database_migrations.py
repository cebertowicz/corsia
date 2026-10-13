"""The migration runner's safety properties, proven rather than asserted.

Each test here corresponds to a way an unattended startup migration can
destroy a production database. The rollback tests in particular exist because
Python's ``sqlite3`` autocommits DDL: the naive ``with conn:`` shape silently
leaves a half-migrated database behind, which is only discovered later.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from infra.database_migrations import (
    MIGRATIONS,
    MigrationError,
    migrate_to_current,
)


@pytest.fixture
def ladder():
    """Register migration steps for one test and restore the real ladder."""
    original = dict(MIGRATIONS)
    MIGRATIONS.clear()
    yield MIGRATIONS
    MIGRATIONS.clear()
    MIGRATIONS.update(original)


def _database(tmp_path: Path, version: int) -> Path:
    """A small populated database at ``version``, with a foreign key to break."""
    path = tmp_path / "test.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE parent (id INTEGER PRIMARY KEY, label TEXT);
        CREATE TABLE child (
            id INTEGER PRIMARY KEY,
            parent_id INTEGER NOT NULL REFERENCES parent(id)
        );
        INSERT INTO parent (id, label) VALUES (1, 'kept');
        INSERT INTO child (id, parent_id) VALUES (10, 1);
        """
    )
    conn.execute(f"PRAGMA user_version={version}")
    conn.commit()
    conn.close()
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _backups(tmp_path: Path) -> list[Path]:
    return sorted(tmp_path.glob("*.pre-migration-*.bak"))


def test_failed_migration_leaves_the_database_byte_identical(tmp_path, ladder):
    """The whole point: a step that fails mid-ladder must undo its own DDL.

    Under the naive ``with conn:`` shape this test fails — the table created
    before the raise survives, because sqlite3 autocommits DDL.
    """
    def creates_then_fails(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE half_migrated (x)")
        conn.execute("INSERT INTO parent (id, label) VALUES (2, 'added')")
        raise RuntimeError("step exploded")

    ladder[1] = creates_then_fails
    path = _database(tmp_path, 1)
    before = _digest(path)

    conn = sqlite3.connect(path)
    try:
        with pytest.raises(MigrationError, match="rolled back, database unchanged"):
            migrate_to_current(conn, 2)
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "half_migrated" not in tables
        assert conn.execute("SELECT count(*) FROM parent").fetchone()[0] == 1
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 1
    finally:
        conn.close()

    assert _digest(path) == before


def test_a_migration_that_breaks_referential_integrity_is_rolled_back(tmp_path, ladder):
    """Validation runs inside the transaction, so a bad step never commits."""
    def orphans_a_child(conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM parent WHERE id=1")

    ladder[1] = orphans_a_child
    path = _database(tmp_path, 1)
    before = _digest(path)

    conn = sqlite3.connect(path)
    try:
        with pytest.raises(MigrationError, match="foreign_key_check failed"):
            migrate_to_current(conn, 2)
        assert conn.execute("SELECT count(*) FROM parent").fetchone()[0] == 1
    finally:
        conn.close()

    assert _digest(path) == before


def test_a_migration_with_a_mismatched_rebuilt_table_index_is_rolled_back(
    tmp_path, ladder
):
    """A rebuilt table's indexes receive its table-scoped integrity check."""
    def rebuilds_child_with_mismatched_index(conn: sqlite3.Connection) -> None:
        conn.execute("ALTER TABLE child RENAME TO old_child")
        conn.execute(
            "CREATE TABLE child ("
            "id INTEGER PRIMARY KEY, "
            "parent_id INTEGER NOT NULL REFERENCES parent(id)"
            ")"
        )
        conn.execute("INSERT INTO child SELECT * FROM old_child")
        conn.execute("DROP TABLE old_child")
        conn.execute("CREATE INDEX child_parent_idx ON child(parent_id)")
        # Simulate an index definition that no longer matches its b-tree. This
        # is the corruption table-scoped integrity_check must catch; quick_check
        # deliberately does not compare table rows to index entries.
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute(
            "UPDATE sqlite_schema SET sql=? WHERE name='child_parent_idx'",
            ("CREATE INDEX child_parent_idx ON child(id)",),
        )
        conn.execute("PRAGMA writable_schema=RESET")

    ladder[1] = rebuilds_child_with_mismatched_index
    path = _database(tmp_path, 1)
    before = _digest(path)

    conn = sqlite3.connect(path)
    try:
        with pytest.raises(MigrationError) as raised:
            migrate_to_current(conn, 2)
        assert str(raised.value.__cause__) == (
            "integrity_check(child) failed after migration: "
            "row 1 missing from index child_parent_idx"
        )
    finally:
        conn.close()

    assert _digest(path) == before


def test_a_passing_migration_checks_only_changed_tables(tmp_path, ladder):
    """Unchanged tables do not pay a table-scoped integrity check."""
    ladder[1] = lambda conn: conn.execute("CREATE TABLE added (id INTEGER PRIMARY KEY)")
    path = _database(tmp_path, 1)
    statements: list[str] = []

    conn = sqlite3.connect(path)
    conn.set_trace_callback(statements.append)
    try:
        assert migrate_to_current(conn, 2).migrated
    finally:
        conn.close()

    integrity_checks = [
        statement
        for statement in statements
        if statement.startswith("PRAGMA integrity_check(")
    ]
    assert integrity_checks == ['PRAGMA integrity_check("added")']


def test_a_rebuild_with_identical_ddl_still_checks_the_table(tmp_path, ladder):
    """A table rebuilt from the same definition text is still validated."""
    def rebuilds_child_identically(conn: sqlite3.Connection) -> None:
        ddl = conn.execute(
            "SELECT sql FROM sqlite_schema WHERE name='child'"
        ).fetchone()[0]
        conn.execute("ALTER TABLE child RENAME TO child_old")
        conn.execute(ddl)
        conn.execute("INSERT INTO child SELECT * FROM child_old")
        conn.execute("DROP TABLE child_old")

    ladder[1] = rebuilds_child_identically
    path = _database(tmp_path, 1)
    statements: list[str] = []

    conn = sqlite3.connect(path)
    conn.set_trace_callback(statements.append)
    try:
        assert migrate_to_current(conn, 2).migrated
    finally:
        conn.close()

    assert 'PRAGMA integrity_check("child")' in statements


def test_a_successful_migration_applies_every_step_in_order(tmp_path, ladder):
    """Multi-step ladders run in sequence and land on the target version."""
    applied: list[int] = []

    def step(version: int):
        def run(conn: sqlite3.Connection) -> None:
            applied.append(version)
            conn.execute(f"CREATE TABLE step_{version} (x)")
        return run

    ladder[1] = step(1)
    ladder[2] = step(2)
    path = _database(tmp_path, 1)

    conn = sqlite3.connect(path)
    try:
        result = migrate_to_current(conn, 3)
        assert applied == [1, 2]
        assert result.migrated is True
        assert (result.from_version, result.to_version) == (1, 3)
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 3
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"step_1", "step_2"} <= tables
    finally:
        conn.close()


def test_an_already_current_database_is_untouched(tmp_path, ladder):
    """Re-running against a current database is a no-op, and takes no backup."""
    ladder[1] = lambda conn: conn.execute("CREATE TABLE should_not_run (x)")
    path = _database(tmp_path, 2)
    before = _digest(path)

    conn = sqlite3.connect(path)
    try:
        result = migrate_to_current(conn, 2)
        assert result.migrated is False
        assert result.backup_path is None
    finally:
        conn.close()

    assert _digest(path) == before
    assert _backups(tmp_path) == []


def test_an_unreachable_version_refuses_before_writing_a_backup(tmp_path, ladder):
    """No registered step means refuse — and refuse before any side effect."""
    path = _database(tmp_path, 1)
    before = _digest(path)

    conn = sqlite3.connect(path)
    try:
        with pytest.raises(MigrationError, match="no migration registered"):
            migrate_to_current(conn, 2)
    finally:
        conn.close()

    assert _digest(path) == before
    assert _backups(tmp_path) == []


def test_a_newer_database_is_refused_rather_than_downgraded(tmp_path, ladder):
    """A database written by a newer build must not be rolled backwards."""
    path = _database(tmp_path, 9)
    before = _digest(path)

    conn = sqlite3.connect(path)
    try:
        with pytest.raises(MigrationError, match="newer than this build"):
            migrate_to_current(conn, 2)
    finally:
        conn.close()

    assert _digest(path) == before
    assert _backups(tmp_path) == []


def test_a_backup_is_written_beside_the_database_that_is_migrating(tmp_path, ladder):
    """The backup binds to the open connection, not a re-resolved path."""
    ladder[1] = lambda conn: conn.execute("CREATE TABLE migrated (x)")
    path = _database(tmp_path, 1)

    conn = sqlite3.connect(path)
    try:
        result = migrate_to_current(conn, 2)
    finally:
        conn.close()

    assert result.backup_path is not None
    assert result.backup_path.parent == path.parent
    backup = sqlite3.connect(result.backup_path)
    try:
        # The backup holds the pre-migration state: old version, no new table.
        assert int(backup.execute("PRAGMA user_version").fetchone()[0]) == 1
        assert backup.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='migrated'"
        ).fetchone()[0] == 0
        assert backup.execute("SELECT label FROM parent WHERE id=1").fetchone()[0] == "kept"
    finally:
        backup.close()


def test_two_concurrent_starts_do_not_both_migrate(tmp_path, ladder):
    """The version is re-read under the write lock, so the loser stands down.

    Deciding to migrate outside the transaction is a check-then-act race: both
    starters would read the old version and both apply the ladder.
    """
    runs: list[str] = []
    both_connected = threading.Barrier(2, timeout=5)

    def counted(conn: sqlite3.Connection) -> None:
        runs.append("applied")
        conn.execute("CREATE TABLE migrated_once (x)")

    ladder[1] = counted
    path = _database(tmp_path, 1)
    results: dict[str, object] = {}

    def start(name: str) -> None:
        # Production's ordinary busy timeout, not a generous one: a large value
        # here would hide a runner that fails to widen the wait for itself.
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            both_connected.wait()
            results[name] = migrate_to_current(conn, 2)
        except BaseException as error:  # surfaced by the assertions below
            results[name] = error
        finally:
            conn.close()

    threads = [threading.Thread(target=start, args=(n,)) for n in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert runs == ["applied"], f"ladder ran {len(runs)} times, expected once"
    outcomes = [results["a"], results["b"]]
    for outcome in outcomes:
        assert not isinstance(outcome, BaseException), outcome
    assert sorted(bool(o.migrated) for o in outcomes) == [False, True]

    # Exactly one backup: the loser must not write a second copy, and must not
    # overwrite the winner's with a snapshot taken after the migration landed.
    # Taking the backup before the write lock produces both of those failures.
    assert len(_backups(tmp_path)) == 1, _backups(tmp_path)
    winner = next(o for o in outcomes if o.migrated)
    loser = next(o for o in outcomes if not o.migrated)
    assert winner.backup_path is not None
    assert loser.backup_path is None

    backup = sqlite3.connect(winner.backup_path)
    try:
        assert int(backup.execute("PRAGMA user_version").fetchone()[0]) == 1
        assert backup.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='migrated_once'"
        ).fetchone()[0] == 0
    finally:
        backup.close()

    conn = sqlite3.connect(path)
    try:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == 2
    finally:
        conn.close()


def test_a_slow_migration_does_not_kill_a_concurrent_starter(tmp_path, ladder):
    """The loser waits for a long migration instead of dying on `database is locked`.

    Production sets `busy_timeout=5000`, sized for ordinary queries, while a
    migration holds its write lock for a backup plus every step — minutes, on a
    database the size of the real one. At the ordinary timeout the second
    backend to start raises `OperationalError: database is locked` rather than
    waiting and standing down, which reads like corruption at exactly the wrong
    moment. Here the timeout is tiny and the migration merely slow, so the same
    ratio is reproduced in a couple of seconds.
    """
    hold = threading.Event()
    entered = threading.Event()

    def slow(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE slow_migrated (x)")
        entered.set()
        hold.wait(timeout=10)

    ladder[1] = slow
    path = _database(tmp_path, 1)
    results: dict[str, object] = {}

    def winner() -> None:
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA busy_timeout=100")
        try:
            results["winner"] = migrate_to_current(conn, 2)
        except BaseException as error:
            results["winner"] = error
        finally:
            conn.close()

    def loser() -> None:
        conn = sqlite3.connect(path)
        # Deliberately far too small for the migration: this is the setting the
        # runner has to override, and the one that masked the defect when the
        # test picked a generous value instead.
        conn.execute("PRAGMA busy_timeout=100")
        try:
            entered.wait(timeout=10)
            results["loser"] = migrate_to_current(conn, 2)
        except BaseException as error:
            results["loser"] = error
        finally:
            conn.close()

    threads = [threading.Thread(target=winner), threading.Thread(target=loser)]
    threads[0].start()
    threads[1].start()
    entered.wait(timeout=10)
    time.sleep(1.5)  # keep the lock held well past the 100ms busy timeout
    hold.set()
    for thread in threads:
        thread.join(timeout=30)

    for name in ("winner", "loser"):
        assert not isinstance(results[name], BaseException), (
            f"{name} raised instead of completing: {results[name]!r}"
        )
    assert results["winner"].migrated is True
    assert results["loser"].migrated is False


def test_the_connection_busy_timeout_is_restored_after_migrating(tmp_path, ladder):
    """The migration's long lock wait must not leak into ordinary queries."""
    ladder[1] = lambda conn: conn.execute("CREATE TABLE migrated (x)")
    path = _database(tmp_path, 1)

    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        migrate_to_current(conn, 2)
        assert int(conn.execute("PRAGMA busy_timeout").fetchone()[0]) == 5000
    finally:
        conn.close()


def test_an_in_memory_database_migrates_without_a_backup(ladder):
    """A memory database has no file to lose, so it is not a refusal case."""
    ladder[1] = lambda conn: conn.execute("CREATE TABLE migrated (x)")
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        conn.execute("PRAGMA user_version=1")
        result = migrate_to_current(conn, 2)
        assert result.migrated is True
        assert result.backup_path is None
    finally:
        conn.close()


# --- the backup must not become the outage -----------------------------------


def _fail_every_time(_conn):
    raise RuntimeError("migration step fails every time")


def _attempt(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        migrate_to_current(conn, 2)
    finally:
        conn.close()


class _RestartClock:
    """Distinct backup stamps, the way separate crash-restarts produce them.

    The stamp has second resolution, so a tight in-process loop reuses one
    filename and trips the refuse-to-overwrite guard — which is not the bug.
    Production restarts were ~11 seconds apart, each with its own filename and
    nothing stopping it; that is what these tests have to reproduce.
    """

    def __init__(self) -> None:
        self._now = datetime(2026, 9, 9, 23, 0, 0, tzinfo=timezone.utc)

    def now(self, tz=None):
        self._now += timedelta(seconds=11)
        return self._now


@pytest.fixture
def restart_clock(monkeypatch):
    import infra.database_migrations as migrations

    monkeypatch.setattr(migrations, "datetime", _RestartClock())
    return None


def _shrinking_disk(*sizes: int):
    """Report free space that falls as copies land, the way a real disk does."""
    remaining = list(sizes)

    def usage(_path):
        free = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return SimpleNamespace(total=free, used=0, free=free)

    return usage


def test_a_crash_restart_loop_halts_before_the_host_runs_out(
    tmp_path, ladder, restart_clock, monkeypatch,
):
    """The write lock cannot bound backups across boots, so the reserve must.

    Nine 4.27 GB copies in four minutes took a host from ~41 GiB free to 3.4
    GiB, which would have taken down the host, the second instance and the CI
    runners with it. The lock makes only one starter the winner within a single
    boot; under a supervisor that restarts on crash, every retry is a fresh
    uncontested winner, and because the filename carries a timestamp the
    refuse-to-overwrite guard never fires.

    The loop must stop while the host is still usable -- not at the last free
    byte, and not by counting files, which says nothing about whether an older
    backup still represents the state a new attempt would protect.
    """
    import infra.database_migrations as migrations

    path = _database(tmp_path, 1)
    ladder[1] = _fail_every_time
    before = _digest(path)
    required = (
        int(migrations._database_bytes(path) * migrations._BACKUP_MIN_FREE_RATIO)
        + migrations._BACKUP_FREE_FLOOR_BYTES
    )
    # Room for two copies above the reserve, then not enough for a third.
    monkeypatch.setattr(
        migrations.shutil,
        "disk_usage",
        _shrinking_disk(required + 1, required + 1, required - 1),
    )

    for _ in range(5):
        with pytest.raises(MigrationError):
            _attempt(path)

    # The third attempt freed the oldest superseded backup and still lacked
    # room; the newest is never pruned, so later attempts refuse with it intact.
    assert [p.name for p in _backups(tmp_path)] == [
        "test.db.pre-migration-v1-20260909T230022Z.bak"
    ]
    assert _digest(path) == before


def test_accumulated_backups_alone_never_refuse_a_needed_one(
    tmp_path, ladder, restart_clock,
):
    """Counting backups would be cheaper and is unsound, so it is not done.

    A failed migration rolls back and the previous build resumes serving, so
    the database keeps changing: an older backup for the same source version
    stops representing the state a later attempt would protect. Refusing on
    that count can refuse a copy that is genuinely needed, and leave startup
    blocked until someone picks a file to delete. With room on disk, an attempt
    always takes its own backup however many already exist.
    """
    path = _database(tmp_path, 1)
    ladder[1] = _fail_every_time

    for _ in range(4):
        with pytest.raises(MigrationError):
            _attempt(path)
        # The previous build resumes serving between attempts, so the state a
        # backup must protect is not the state the last one captured.
        conn = sqlite3.connect(path)
        try:
            conn.execute("INSERT INTO parent (label) VALUES ('served')")
            conn.commit()
        finally:
            conn.close()

    assert len(_backups(tmp_path)) == 4


def test_a_backup_that_would_not_fit_refuses_before_copying(
    tmp_path, ladder, monkeypatch,
):
    """A backup that fills the filesystem takes down everything sharing it."""
    import infra.database_migrations as migrations

    path = _database(tmp_path, 1)
    ladder[1] = lambda conn: conn.execute("INSERT INTO parent (id) VALUES (2)")
    before = _digest(path)
    monkeypatch.setattr(
        migrations.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=1, used=1, free=1),
    )

    with pytest.raises(MigrationError) as refusal:
        _attempt(path)

    message = str(refusal.value)
    assert "not enough free space" in message
    # Both numbers and recovery targets, so the condition can be cleared without guesswork.
    assert "GB free" in message and "GB required" in message
    assert "GB database" in message
    assert f"Superseded backups in {tmp_path}" in message
    assert _backups(tmp_path) == []
    assert _digest(path) == before
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        conn.close()


def test_a_refusal_before_copying_never_claims_a_backup(
    tmp_path, ladder, restart_clock, monkeypatch,
):
    """False recovery state is worse than none, and lands during an incident.

    The refusal happens *before* any copy, so `backup_path` is still None and
    the handler used to report "A pre-migration backup is at None". An operator
    reading that mid-incident is being told a recovery artifact exists when it
    does not.
    """
    import infra.database_migrations as migrations

    path = _database(tmp_path, 1)
    ladder[1] = _fail_every_time
    monkeypatch.setattr(
        migrations.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=1, used=1, free=1),
    )

    with pytest.raises(MigrationError) as refused:
        _attempt(path)

    assert "backup is at None" not in str(refused.value)
    assert "No pre-migration backup was written." in str(refused.value)


def test_a_space_refusal_never_claims_a_backup(tmp_path, ladder, monkeypatch):
    """The same honesty on the other pre-copy refusal path."""
    import infra.database_migrations as migrations

    path = _database(tmp_path, 1)
    ladder[1] = lambda conn: conn.execute("INSERT INTO parent (id) VALUES (2)")
    monkeypatch.setattr(
        migrations.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=1, used=1, free=1),
    )

    with pytest.raises(MigrationError) as refusal:
        _attempt(path)

    assert "backup is at None" not in str(refusal.value)
    assert "No pre-migration backup was written." in str(refusal.value)
    assert _backups(tmp_path) == []


def test_a_real_failure_after_a_backup_still_names_it(tmp_path, ladder):
    """The honest branch must not cost the operator the path when it exists."""
    path = _database(tmp_path, 1)
    ladder[1] = _fail_every_time

    with pytest.raises(MigrationError) as failure:
        _attempt(path)

    written = _backups(tmp_path)
    assert len(written) == 1
    assert f"A pre-migration backup is at {written[0]}." in str(failure.value)


# --- pre-migration backups are bounded, not accumulated -----------------------


def _seed_backups(directory: Path, *names: str) -> None:
    for name in names:
        (directory / name).write_bytes(b"old backup")


def _names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


_LOOK_ALIKES = (
    "other.db.pre-migration-v3-20260101T000000Z.bak",
    "test.db.pre-migration-vX-20260101T000000Z.bak",
    "test.db.pre-migration-v3-20260101T000000Z.bak.keep",
    "test.db.archive-20260101T000000Z.json",
)


def _seed_look_alikes(directory: Path) -> None:
    _seed_backups(directory, *_LOOK_ALIKES)
    (directory / "backups").mkdir()
    (directory / "backups" / "daily.db").write_bytes(b"daily")


def test_a_successful_migration_keeps_only_the_newest_backups(
    tmp_path, ladder, restart_clock,
):
    ladder[1] = lambda conn: conn.execute("CREATE TABLE migrated (x)")
    path = _database(tmp_path, 1)
    _seed_look_alikes(tmp_path)
    _seed_backups(
        tmp_path,
        "test.db.pre-migration-v10-20260101T000000Z.bak",
        "test.db.pre-migration-v11-20260201T000000Z.bak",
        "test.db.pre-migration-v12-20260301T000000Z.bak",
        "test.db.pre-migration-v9-20260401T000000Z.bak",
    )

    _attempt(path)

    assert _names(tmp_path) == sorted(
        [
            "test.db",
            "backups",
            "test.db.pre-migration-v9-20260401T000000Z.bak",
            "test.db.pre-migration-v1-20260909T230011Z.bak",
            *_LOOK_ALIKES,
        ]
    )
    assert (tmp_path / "backups" / "daily.db").read_bytes() == b"daily"


def test_the_keep_count_is_overridable(tmp_path, ladder, restart_clock, monkeypatch):
    import infra.database_migrations as migrations

    monkeypatch.setattr(migrations, "_PRE_MIGRATION_BACKUPS_KEEP", 1)
    ladder[1] = lambda conn: conn.execute("CREATE TABLE migrated (x)")
    path = _database(tmp_path, 1)
    _seed_backups(tmp_path, "test.db.pre-migration-v10-20260101T000000Z.bak")

    _attempt(path)

    assert _names(tmp_path) == [
        "test.db",
        "test.db.pre-migration-v1-20260909T230011Z.bak",
    ]


def test_a_failed_migration_prunes_nothing(tmp_path, ladder, restart_clock):
    ladder[1] = _fail_every_time
    path = _database(tmp_path, 1)
    seeded = [
        "test.db.pre-migration-v10-20260101T000000Z.bak",
        "test.db.pre-migration-v11-20260201T000000Z.bak",
        "test.db.pre-migration-v12-20260301T000000Z.bak",
    ]
    _seed_backups(tmp_path, *seeded)

    with pytest.raises(MigrationError):
        _attempt(path)

    assert _names(tmp_path) == sorted(
        ["test.db", "test.db.pre-migration-v1-20260909T230011Z.bak", *seeded]
    )


def test_short_room_prunes_oldest_superseded_backups_then_migrates(
    tmp_path, ladder, restart_clock, monkeypatch,
):
    import infra.database_migrations as migrations

    ladder[1] = lambda conn: conn.execute("CREATE TABLE migrated (x)")
    path = _database(tmp_path, 1)
    _seed_look_alikes(tmp_path)
    _seed_backups(
        tmp_path,
        "test.db.pre-migration-v10-20260101T000000Z.bak",
        "test.db.pre-migration-v11-20260201T000000Z.bak",
        "test.db.pre-migration-v12-20260301T000000Z.bak",
        "test.db.pre-migration-v13-20260401T000000Z.bak",
    )
    required = (
        int(migrations._database_bytes(path) * migrations._BACKUP_MIN_FREE_RATIO)
        + migrations._BACKUP_FREE_FLOOR_BYTES
    )
    # Short on the first check and after the first deletion, enough after the second.
    monkeypatch.setattr(
        migrations.shutil,
        "disk_usage",
        _shrinking_disk(required - 1, required - 1, required + 1),
    )

    _attempt(path)

    assert _names(tmp_path) == sorted(
        [
            "test.db",
            "backups",
            "test.db.pre-migration-v13-20260401T000000Z.bak",
            "test.db.pre-migration-v1-20260909T230011Z.bak",
            *_LOOK_ALIKES,
        ]
    )


def test_short_room_with_only_the_newest_backup_refuses_as_before(
    tmp_path, ladder, restart_clock, monkeypatch,
):
    ladder[1] = lambda conn: conn.execute("CREATE TABLE migrated (x)")
    path = _database(tmp_path, 1)
    _seed_backups(tmp_path, "test.db.pre-migration-v10-20260101T000000Z.bak")
    import infra.database_migrations as migrations

    monkeypatch.setattr(
        migrations.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=1, used=1, free=1),
    )

    with pytest.raises(MigrationError, match="not enough free space"):
        _attempt(path)

    assert _names(tmp_path) == ["test.db", "test.db.pre-migration-v10-20260101T000000Z.bak"]


def test_pruning_logs_one_info_line_with_bytes_reclaimed(
    tmp_path, ladder, restart_clock, caplog,
):
    ladder[1] = lambda conn: conn.execute("CREATE TABLE migrated (x)")
    path = _database(tmp_path, 1)
    _seed_backups(
        tmp_path,
        "test.db.pre-migration-v10-20260101T000000Z.bak",
        "test.db.pre-migration-v11-20260201T000000Z.bak",
        "test.db.pre-migration-v12-20260301T000000Z.bak",
    )

    with caplog.at_level("INFO", logger="infra.database_migrations"):
        _attempt(path)

    pruned = [r.getMessage() for r in caplog.records if "Pruned" in r.getMessage()]
    assert pruned == [
        "Pruned 2 pre-migration backup(s), reclaimed 20 bytes (retention after migration)"
    ]


def test_only_this_databases_backups_match(tmp_path):
    import infra.database_migrations as migrations

    _seed_look_alikes(tmp_path)
    _seed_backups(
        tmp_path,
        "test.db-wal",
        "test.db-shm",
        "test.db.pre-migration-v10-20260301T000000Z.bak",
        "test.db.pre-migration-v9-20260101T000000Z.bak",
    )
    (tmp_path / "test.db.pre-migration-v8-20250101T000000Z.bak").mkdir()

    assert [p.name for p in migrations._pre_migration_backups(tmp_path / "test.db")] == [
        "test.db.pre-migration-v9-20260101T000000Z.bak",
        "test.db.pre-migration-v10-20260301T000000Z.bak",
    ]
