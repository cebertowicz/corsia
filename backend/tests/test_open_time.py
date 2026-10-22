"""Open time: the one server number for how long a run or plan step has had work in it.

Each case is a worked example from the run-time concept page, read from storage
alone. Outside waits come from Plans' read, faked here with its exact signature.
"""

from __future__ import annotations

from datetime import UTC, datetime
import sqlite3

import pytest

import infra.open_time as open_time
from plans import public

DAY = "2026-10-21"


def at(hhmm: str, day: str = DAY) -> datetime:
    hour, minute = hhmm.split(":")
    return datetime.fromisoformat(f"{day}T{int(hour):02d}:{minute}:00").replace(tzinfo=UTC)


def minutes(result: dict) -> float:
    return result["seconds"] / 60


@pytest.fixture
def db(monkeypatch, tmp_path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "state.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, spawned_by TEXT)")
    conn.execute(
        "CREATE TABLE active_turns (id TEXT PRIMARY KEY, thread_id TEXT, status TEXT, "
        "started_at TEXT, ended_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO threads VALUES (?,?)",
        [("lead", None), ("worker", "lead"), ("grandchild", "worker"), ("worker2", "lead"), ("other", None)],
    )
    monkeypatch.setattr(public, "open_wait_intervals", lambda ids, **_: {i: [] for i in ids}, raising=False)
    return conn


def turn(db, thread: str, start: str, end: str | None, day: str = DAY) -> None:
    stamp = lambda hhmm: at(hhmm, day).replace(tzinfo=None).isoformat()  # noqa: E731 - storage is naive UTC
    count = db.execute("SELECT COUNT(*) FROM active_turns").fetchone()[0]
    db.execute(
        "INSERT INTO active_turns VALUES (?,?,?,?,?)",
        (f"t{count}", thread, "completed" if end else "running", stamp(start), stamp(end) if end else None),
    )


def waits(monkeypatch, per_thread: dict[str, list[tuple[str, str | None]]]) -> None:
    """Fake Plans' read: epoch intervals, an open one ending at `until`."""

    def fake(thread_ids, *, since, until, db):
        out = {}
        for thread_id in thread_ids:
            out[thread_id] = [
                (at(a).timestamp(), at(b).timestamp() if b else until.timestamp())
                for a, b in per_thread.get(thread_id, [])
            ]
        return out

    monkeypatch.setattr(public, "open_wait_intervals", fake, raising=False)


def read(db, since: str, until: str | None, as_of: str, day: str = DAY) -> dict:
    return open_time.open_time(
        db, "lead", since=at(since, day), until=at(until, day) if until else None, as_of=at(as_of, day),
    )


def test_a_worker_returning_to_its_lead_never_drops_the_count(db) -> None:
    # Concept page "Real": handed off 9:22, lead's turn ends 9:23, worker runs to 9:27 and wakes the lead.
    turn(db, "lead", "21:22", "21:23")
    turn(db, "worker", "21:22", "21:27")
    assert minutes(read(db, "21:22", None, "21:27")) == 5
    turn(db, "lead", "21:27", None)
    after = read(db, "21:22", None, "21:28")
    assert minutes(after) == 6 and after["open_now"] is True


def test_a_worker_running_through_an_unrelated_wake_counts_once(db) -> None:
    turn(db, "worker", "10:00", "10:10")
    turn(db, "lead", "10:03", "10:05")
    assert minutes(read(db, "10:00", "10:10", "10:30")) == 10


def test_two_overlapping_workers_count_the_union(db) -> None:
    turn(db, "worker", "10:00", "10:06")
    turn(db, "worker2", "10:04", "10:09")
    turn(db, "grandchild", "10:08", "10:12")
    assert minutes(read(db, "10:00", "10:12", "10:30")) == 12


def test_an_unrelated_thread_is_not_a_descendant(db) -> None:
    turn(db, "other", "10:00", "10:30")
    turn(db, "lead", "10:00", "10:02")
    assert minutes(read(db, "10:00", "10:30", "10:30")) == 2


def test_an_outside_wait_counts_through_the_whole_run(db, monkeypatch) -> None:
    # Concept page "Constructed": 3 min of work, a wait for CI from 2:03 to 2:17, 4 more minutes of work.
    turn(db, "lead", "14:00", "14:03")
    waits(monkeypatch, {"lead": [("14:03", "14:17")]})
    turn(db, "lead", "14:17", "14:21")
    readings = [minutes(read(db, "14:00", None, at_)) for at_ in ("14:03", "14:04", "14:17", "14:18")]
    assert readings == [3, 4, 17, 18]
    final = read(db, "14:00", "14:21", "14:30")
    assert minutes(final) == 21 and final["open_now"] is False


def test_a_wait_open_now_keeps_the_run_open(db, monkeypatch) -> None:
    waits(monkeypatch, {"lead": [("14:00", None)]})
    turn(db, "lead", "13:57", "14:00")
    result = read(db, "13:57", None, "14:04")
    assert minutes(result) == 7 and result["open_now"] is True


def test_an_idle_gap_does_not_count(db) -> None:
    # Concept page "Contrast": reply at 3:00 owing nothing, an unrelated wake at 8:00 for 2 minutes.
    turn(db, "lead", "14:57", "15:00")
    turn(db, "lead", "20:00", "20:02")
    result = read(db, "14:57", "20:02", "21:00")
    assert minutes(result) == 5 and result["open_now"] is False


def test_a_set_aside_stretch_is_not_open(db, monkeypatch) -> None:
    # Plans' read already leaves set-aside and paused time out; nothing is added for it.
    waits(monkeypatch, {"lead": []})
    turn(db, "lead", "09:00", "09:02")
    turn(db, "lead", "17:00", "17:03")
    assert minutes(read(db, "09:00", "17:03", "18:00")) == 5


def test_a_range_clips_a_turn_that_straddles_it(db) -> None:
    turn(db, "lead", "09:50", "10:10")
    assert minutes(read(db, "10:00", "10:05", "11:00")) == 5


def test_a_reload_answers_the_same_from_storage_alone(db, tmp_path) -> None:
    turn(db, "lead", "10:00", "10:03")
    turn(db, "worker", "10:02", "10:08")
    first = read(db, "10:00", None, "10:08")
    db.commit()
    reopened = sqlite3.connect(tmp_path / "state.sqlite")
    reopened.row_factory = sqlite3.Row
    assert read(reopened, "10:00", None, "10:08") == first
    assert minutes(first) == 8


def test_the_number_never_goes_back_while_a_run_is_open(db, monkeypatch) -> None:
    waits(monkeypatch, {"lead": [("10:05", "10:15")]})
    turn(db, "lead", "10:00", "10:05")
    turn(db, "worker", "10:02", "10:07")
    turn(db, "lead", "10:15", None)
    seen = [read(db, "10:00", None, f"10:{minute:02d}")["seconds"] for minute in range(0, 31, 1)]
    assert seen == sorted(seen)
    assert seen[-1] == 30 * 60
