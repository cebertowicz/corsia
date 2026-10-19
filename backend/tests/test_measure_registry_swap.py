"""The registry swaps whole states: a settle during a rebuild, or a racing first load, never sees a partial one.

Two syncs of one file plan outside the transaction; the primary key refuses the one that planned
from stale versions, and it re-plans once.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

import infra.measure_registry as registry
from infra.measure_admission import admit
from infra.measure_registry import REGISTRY, sync
from infra.outcome_events_schema import OUTCOME_EVENTS_SCHEMA_SQL
from tests.test_measure_registry import MEASURES, own

WAIT = 10


class PausingDb:
    """A connection whose declaration read stops after its first row until the test lets it go."""

    def __init__(self, real: sqlite3.Connection, gate: threading.Event, reached: threading.Event) -> None:
        self.real, self.gate, self.reached = real, gate, reached

    def execute(self, sql: str, *args):
        cursor = self.real.execute(sql, *args)
        return self._paused(cursor) if "FROM measure_definitions" in sql else cursor

    def _paused(self, cursor) -> Iterator[tuple]:
        for index, row in enumerate(cursor):
            if index == 1:
                self.reached.set()
                assert self.gate.wait(WAIT)
            yield row


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


@pytest.fixture
def database(tmp_path: Path):
    """A file database holding two agents' declared events, and a registry loaded from it."""
    path = tmp_path / "registry.db"
    seed = connect(path)
    seed.executescript(OUTCOME_EVENTS_SCHEMA_SQL)
    REGISTRY.reset()
    sync(seed, owner_thread_id="agent-a", path=own("agent-a"), text=MEASURES)
    sync(seed, owner_thread_id="agent-b", path=own("agent-b"), text=MEASURES.replace("namespace: mail", "namespace: shop").replace("mail.triaged", "shop.triaged"))
    assert set(REGISTRY.events) == {"mail.triaged", "shop.triaged"}
    spec = json.dumps({"attributes": {"message_ref": "id"}, "dedupe": ["message_ref"]})
    seed.execute("INSERT INTO measure_definitions VALUES ('agent-a','event','mail.late',1,?,?,'sha','t',0)", (spec, own("agent-a")))
    seed.commit()
    yield path, seed
    seed.close()


def in_thread(call) -> tuple[threading.Thread, list]:
    result: list = []
    thread = threading.Thread(target=lambda: result.append(call()), daemon=True)
    thread.start()
    return thread, result


def test_a_settle_during_a_rebuild_sees_the_old_complete_registry_then_the_new_one(database) -> None:
    path, seed = database
    gate, reached = threading.Event(), threading.Event()
    rebuilding = connect(path)
    thread, _ = in_thread(lambda: REGISTRY.rebuild(PausingDb(rebuilding, gate, reached)))
    assert reached.wait(WAIT)  # the rebuild has read one declaration and is mid-read
    try:
        assert set(REGISTRY.events) == {"mail.triaged", "shop.triaged"}  # whole old state: nothing emptied, nothing half-filled
        admitted = admit(seed, owner_thread_id="agent-a", event_name="mail.triaged", attributes={"message_ref": "m1", "action": "replied"}, run_id="r")
        assert admitted.refusal is None and admitted.result and admitted.result.inserted
        assert seed.execute("SELECT coalesce(sum(n),0) FROM outcome_event_counters WHERE counter='name_undeclared'").fetchone()[0] == 0
    finally:
        gate.set()
        thread.join(WAIT)
    assert set(REGISTRY.events) == {"mail.triaged", "shop.triaged", "mail.late"}
    rebuilding.close()


def test_a_first_load_waits_for_the_load_already_running_instead_of_reading_a_partial_registry(database) -> None:
    path, _ = database
    REGISTRY.reset()
    gate, reached = threading.Event(), threading.Event()
    first_db, second_db = connect(path), connect(path)
    first, first_result = in_thread(lambda: REGISTRY.ensure_loaded(PausingDb(first_db, gate, reached)))
    assert reached.wait(WAIT)
    second, second_result = in_thread(lambda: REGISTRY.ensure_loaded(second_db))
    second.join(0.5)
    assert second.is_alive() and not REGISTRY.loaded  # it takes the same lock; nothing is visible until the load completes
    gate.set()
    first.join(WAIT)
    second.join(WAIT)
    expected = {"mail.triaged", "shop.triaged", "mail.late"}
    assert set(first_result[0].events) == set(second_result[0].events) == expected
    assert first_result[0] is second_result[0]  # one load served both
    first_db.close()
    second_db.close()


def test_two_concurrent_syncs_of_one_file_end_with_one_gapless_version_sequence_and_one_retry(tmp_path: Path, monkeypatch) -> None:
    """The first sync plans from v1 while the second commits v2; the first's insert collides on the
    primary key, rolls back, re-plans from v2 and writes v3. Nothing is lost, duplicated or skipped."""
    path = tmp_path / "sync.db"
    seed = connect(path)
    seed.executescript(OUTCOME_EVENTS_SCHEMA_SQL)
    REGISTRY.reset()
    sync(seed, owner_thread_id="agent-a", path=own("agent-a"), text=MEASURES)
    gate, reached = threading.Event(), threading.Event()
    real_plan = registry._plan
    plans: list[str] = []

    def pausing_plan(*args):
        name = threading.current_thread().name
        plans.append(name)
        if name == "first" and plans.count("first") == 1:
            reached.set()
            assert gate.wait(WAIT)
        return real_plan(*args)

    monkeypatch.setattr(registry, "_plan", pausing_plan)
    results: dict[str, dict] = {}

    def run(text: str) -> None:
        connection = connect(path)
        try:
            results[threading.current_thread().name] = sync(connection, owner_thread_id="agent-a", path=own("agent-a"), text=text)
        finally:
            connection.close()

    first = threading.Thread(target=run, args=(MEASURES.replace("min_n: 20", "min_n: 30"),), name="first", daemon=True)
    first.start()
    assert reached.wait(WAIT)  # it has read v1 and is planning v2, outside any transaction
    second = threading.Thread(target=run, args=(MEASURES.replace("min_n: 20", "min_n: 40"),), name="second", daemon=True)
    second.start()
    second.join(WAIT)
    try:
        assert not second.is_alive()  # planning holds no lock: the second sync commits v2 at once
    finally:
        gate.set()
        first.join(WAIT)
    assert plans == ["first", "second", "first"]  # the first re-planned exactly once
    assert results["second"]["items"][1]["status"] == "changed v1→v2"
    assert results["first"]["items"][1]["status"] == "changed v2→v3"
    rows = seed.execute("SELECT version, json_extract(spec_json, '$.min_n') FROM measure_definitions WHERE name='replied_share' ORDER BY version").fetchall()
    assert rows == [(1, 20), (2, 40), (3, 30)]
    assert seed.execute("SELECT version FROM measure_definitions WHERE name='mail.triaged'").fetchall() == [(1,)]
    seed.close()


def test_a_second_collision_refuses_the_sync_and_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    connection = connect(tmp_path / "sync.db")
    connection.executescript(OUTCOME_EVENTS_SCHEMA_SQL)
    REGISTRY.reset()
    calls: list[int] = []

    def colliding(*_args):
        calls.append(1)
        raise sqlite3.IntegrityError("UNIQUE constraint failed")

    monkeypatch.setattr(registry, "_claim_namespace", colliding)
    with pytest.raises(ValueError, match=r"^measure_sync: another sync of this file ran at the same time; run measure_sync again\.$"):
        sync(connection, owner_thread_id="agent-a", path=own("agent-a"), text=MEASURES)
    assert len(calls) == 2 and not connection.in_transaction
    assert connection.execute("SELECT count(*) FROM measure_definitions").fetchone()[0] == 0
    connection.close()
