"""The startup progress file a slow migration publishes before HTTP answers."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading

import pytest

import infra.startup_progress as startup_progress
from infra.database_migrations import MIGRATIONS, migrate_to_current

# The update shell reads these three fields with this pattern; keep them first.
SHELL_PREFIX = re.compile(r'^\{"schema": 1, "pid": ([0-9]+), "seq": ([0-9]+), "phase": "([a-z_]+)"')


@pytest.fixture(autouse=True)
def stopped():
    startup_progress._state = None
    yield
    startup_progress._state = None


def read(tmp_path) -> dict:
    text = startup_progress.path(tmp_path).read_text()
    assert SHELL_PREFIX.match(text), text
    return json.loads(text)


def test_nothing_publishes_outside_a_startup(tmp_path):
    startup_progress.mark("migrating", done=1, total=2)
    startup_progress.beat()
    startup_progress.fail("boom")
    assert not startup_progress.path(tmp_path).exists()


def test_each_write_advances_seq_under_this_pid(tmp_path):
    startup_progress.begin(tmp_path)
    first = read(tmp_path)
    assert (first["pid"], first["seq"], first["phase"]) == (os.getpid(), 1, "starting")
    startup_progress.mark("migrating", done=0, total=3, from_version=1, to_version=4)
    second = read(tmp_path)
    assert second["seq"] == 2
    assert (second["done"], second["total"], second["from_version"], second["to_version"]) == (0, 3, 1, 4)


def test_beat_is_rate_limited_to_the_heartbeat(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(startup_progress.time, "monotonic", lambda: clock[0])
    startup_progress.begin(tmp_path)
    startup_progress.beat(done=5, total=10)
    assert read(tmp_path)["seq"] == 1
    clock[0] += startup_progress.HEARTBEAT_SECONDS
    startup_progress.beat(done=6, total=10)
    assert (read(tmp_path)["seq"], read(tmp_path)["done"]) == (2, 6)


def test_finish_then_fail_keeps_serving(tmp_path):
    startup_progress.begin(tmp_path)
    startup_progress.finish()
    startup_progress.fail("shutdown")
    assert read(tmp_path)["phase"] == "serving"


def test_fail_publishes_the_reason_once(tmp_path):
    startup_progress.begin(tmp_path)
    startup_progress.fail(RuntimeError("database locked"))
    failed = read(tmp_path)
    assert (failed["phase"], failed["error"]) == ("failed", "database locked")
    startup_progress.mark("recovering")
    assert read(tmp_path)["seq"] == failed["seq"]


def test_an_unwritable_root_never_fails_startup(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("")
    startup_progress.begin(blocked)
    startup_progress.mark("migrating")
    assert startup_progress._state is None


def test_a_failing_heartbeat_stops_publishing_instead_of_aborting_the_statement(tmp_path, monkeypatch):
    def broken(**_counts):
        raise ValueError("unserializable")

    startup_progress.begin(tmp_path)
    monkeypatch.setattr(startup_progress, "beat", broken)
    conn = sqlite3.connect(":memory:")
    startup_progress.watch(conn)
    assert conn.execute(_COUNT_TO_200K).fetchone()[0] == 200_000
    assert startup_progress._state is None


def test_publishing_reports_the_exception_that_stopped_startup(tmp_path):
    with pytest.raises(RuntimeError):
        with startup_progress.publishing(tmp_path):
            raise RuntimeError("schema 150 is newer than this build")
    failed = read(tmp_path)
    assert (failed["phase"], failed["error"]) == ("failed", "schema 150 is newer than this build")
    assert startup_progress._state is None


def test_recovering_work_on_a_pooled_connection_keeps_seq_moving(tmp_path, monkeypatch):
    """Startup work after the data is open beats too, not only the migration."""
    from infra import database

    monkeypatch.setattr(startup_progress, "HEARTBEAT_SECONDS", 0.0)
    monkeypatch.setattr(startup_progress, "SQLITE_PROGRESS_OPS", 10_000)
    startup_progress.begin(tmp_path / "runtime")
    startup_progress.mark("recovering")
    before = read(tmp_path / "runtime")["seq"]
    conn = database._make_connection(str(tmp_path / "corsia.db"))
    try:
        conn.execute(_COUNT_TO_200K).fetchone()
    finally:
        conn.close()
    after = read(tmp_path / "runtime")
    assert after["phase"] == "recovering"
    assert after["seq"] > before + 1


def test_connections_opened_outside_startup_carry_no_handler(tmp_path):
    from infra import database

    calls = []
    conn = database._make_connection(str(tmp_path / "corsia.db"))
    try:
        startup_progress.begin(tmp_path / "runtime")
        startup_progress.mark("recovering")
        seq = read(tmp_path / "runtime")["seq"]
        conn.execute(_COUNT_TO_200K).fetchone()
        calls.append(read(tmp_path / "runtime")["seq"])
    finally:
        conn.close()
    assert calls == [seq]


def test_beats_from_many_threads_leave_one_whole_file(tmp_path, monkeypatch):
    monkeypatch.setattr(startup_progress, "HEARTBEAT_SECONDS", 0.0)
    startup_progress.begin(tmp_path)

    def hammer():
        for done in range(200):
            startup_progress.beat(done=done, total=200)

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    startup_progress.mark("validating")
    final = read(tmp_path)
    assert final["phase"] == "validating" and final["seq"] > 1
    assert [p.name for p in tmp_path.iterdir()] == [startup_progress.FILE_NAME]


_COUNT_TO_200K = (
    "WITH RECURSIVE n(value) AS (SELECT 1 UNION ALL SELECT value+1 FROM n WHERE value < 200000) "
    "SELECT count(*) FROM n"
)


def test_a_migration_publishes_backup_steps_and_heartbeats(tmp_path, monkeypatch):
    """A step that works for a long time keeps seq moving, and the counts land."""
    original = dict(MIGRATIONS)
    phases: list[tuple] = []
    beats = [0]
    real_write = startup_progress._write

    def record():
        real_write()
        if startup_progress._state is not None:
            state = startup_progress._state
            phases.append((state["phase"], state["done"], state["total"]))

    def counting_beat():
        beats[0] += 1
        return 0

    monkeypatch.setattr(startup_progress, "_write", record)
    monkeypatch.setattr(startup_progress, "sqlite_beat", counting_beat)
    monkeypatch.setattr(startup_progress, "SQLITE_PROGRESS_OPS", 1_000)

    def slow_step(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE filler (x)")
        conn.execute(
            "INSERT INTO filler SELECT value FROM ("
            "WITH RECURSIVE n(value) AS (SELECT 1 UNION ALL SELECT value+1 FROM n WHERE value < 20000) "
            "SELECT value FROM n)"
        )

    database = tmp_path / "corsia.db"
    conn = sqlite3.connect(database)
    conn.execute("CREATE TABLE kept (x)")
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    try:
        MIGRATIONS.clear()
        MIGRATIONS[1] = slow_step
        MIGRATIONS[2] = lambda c: c.execute("CREATE TABLE second (x)")
        startup_progress.begin(tmp_path / "runtime")
        assert migrate_to_current(conn, 3).migrated
    finally:
        MIGRATIONS.clear()
        MIGRATIONS.update(original)
        conn.close()
    named = [phase for phase, _done, _total in phases]
    assert named[:2] == ["starting", "backing_up"]
    assert ("migrating", 0, 2) in phases and ("migrating", 2, 2) in phases
    assert named[-1] == "validating"
    assert beats[0] > 1
