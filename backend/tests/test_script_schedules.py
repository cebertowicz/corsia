"""Model-free script schedules: registration, the runner phase, emission."""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from pathlib import Path

import pytest

from automation import schedules, script_runner
from automation import script_schedules as store
from automation.script_readings import build_report, classify
from automation.script_sandbox import STDOUT_CAP_BYTES, ScriptRun, script_command
from infra.schema import SCHEMA_SQL

T0 = "2026-01-01T12:00:00+00:00"
HOUR = 3600


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO threads (id,user_id,created_at,updated_at) "
        "VALUES ('owner','user','t','t')"
    )
    db.commit()
    return db


def _script(tmp_path: Path, body: str = "print('x')") -> Path:
    path = tmp_path / "watch.py"
    path.write_text(body)
    return path


def _register(db, tmp_path, *, series=None, cadence=600, now=T0, thread="owner"):
    path = _script(tmp_path)
    return store.create_script(
        thread,
        "__workspace__",
        script_path=path,
        script_sha256=store.file_sha256(path),
        cadence_seconds=cadence,
        series=series
        or [store.Series("cost", "usd", 0.0, 10.0)],
        timeout_seconds=30,
        now=now,
        db=db,
    )


def _run(stdout: str = "", *, code: int = 0, timed_out: bool = False, stderr: str = ""):
    return ScriptRun(code, stdout.encode(), stderr.encode(), timed_out)


@pytest.fixture(autouse=True)
def _reset_runner(monkeypatch):
    script_runner._in_flight.clear()
    monkeypatch.setattr(script_runner, "finished_at", lambda started: started)
    monkeypatch.setattr(
        script_runner, "_drive_owner", _no_drive, raising=True,
    )


async def _no_drive(_thread_id: str) -> None:
    return None


def _use_run(monkeypatch, run: ScriptRun):
    calls: list[str] = []

    async def fake(thread_id, project_id, path, *, run_id, timeout_seconds):
        calls.append(thread_id)
        return run

    monkeypatch.setattr(script_runner, "run_script", fake)
    return calls


async def _settle_all(db, now):
    started = await script_runner.drive(now, db=db)
    for task in list(script_runner._in_flight.values()):
        await task
    return started


def _wakes(db):
    return db.execute(
        "SELECT content FROM messages WHERE role='system' "
        "AND content LIKE '[EVENT: script.%' ORDER BY rowid"
    ).fetchall()


# --- registration ---------------------------------------------------------


def test_registration_bounds_and_series_validation(tmp_path):
    db = _db()
    with pytest.raises(ValueError, match="cadence_seconds"):
        _register(db, tmp_path, cadence=60)
    with pytest.raises(ValueError, match="min must not exceed max"):
        store.parse_series([{"key": "a", "min": 5, "max": 1}])
    with pytest.raises(ValueError, match="unique"):
        store.parse_series([{"key": "a"}, {"key": "a"}])
    with pytest.raises(ValueError, match="key"):
        store.parse_series([{"key": "Bad Key"}])
    with pytest.raises(ValueError, match="number"):
        store.parse_series([{"key": "a", "max": "9"}])
    with pytest.raises(ValueError, match="finite"):
        store.parse_series([{"key": "a", "max": float("inf")}])
    with pytest.raises(ValueError, match="worse"):
        store.parse_series([{"key": "a", "worse": "sideways"}])
    with pytest.raises(ValueError, match="reduce"):
        store.parse_series([{"key": "a", "reduce": "median"}])
    kept = store.parse_series([{"key": "a", "worse": "up", "reduce": "mean"}])[0]
    assert (kept.worse, kept.reduce) == ("up", "mean")
    assert store.parse_series([{"key": "b"}])[0].reduce == "last"


def test_script_has_no_expiry_and_per_agent_cap(tmp_path):
    db = _db()
    schedule = _register(db, tmp_path)
    assert schedule.kind == "script"
    assert schedule.max_runs == store.UNBOUNDED_RUNS
    for _ in range(store.MAX_PER_AGENT - 1):
        _register(db, tmp_path)
    with pytest.raises(ValueError):
        _register(db, tmp_path)


def test_pin_script_records_sha_and_rejects_out_of_scope(tmp_path, monkeypatch):
    path = _script(tmp_path, "print(1)")
    monkeypatch.setattr("tools.sandbox_config.is_path_allowed", lambda *_a, **_k: True)
    resolved, digest = store.pin_script(str(path), workspace=tmp_path)
    assert resolved == path.resolve()
    assert digest == store.file_sha256(path)
    monkeypatch.setattr("tools.sandbox_config.is_path_allowed", lambda *_a, **_k: False)
    with pytest.raises(ValueError, match="outside"):
        store.pin_script(str(path), workspace=tmp_path)


def test_script_schedule_is_never_a_continuation_wake(tmp_path):
    db = _db()
    _register(db, tmp_path)
    assert schedules.due("2027-01-01T00:00:00+00:00", db=db) == []
    assert [s.kind for s in store.due_scripts("2027-01-01T00:00:00+00:00", db=db)] == [
        "script"
    ]


# --- claim ----------------------------------------------------------------


def test_claim_is_exactly_once_and_skips_missed_slots(tmp_path):
    db = _db()
    schedule = _register(db, tmp_path, cadence=600)
    later = "2026-01-01T15:00:00+00:00"  # many missed slots
    assert store.claim(schedule, later, db=db) is True
    assert store.claim(schedule, later, db=db) is False
    row = schedules.get(schedule.id, db=db)
    assert row.running_since == later
    assert row.next_run_at > later


# --- runner emission ------------------------------------------------------


@pytest.mark.asyncio
async def test_in_bounds_reading_is_a_passive_note_not_a_wake(tmp_path, monkeypatch):
    db = _db()
    schedule = _register(db, tmp_path)
    _use_run(monkeypatch, _run('{"series":"cost","value":3.5}\n'))
    assert await _settle_all(db, T0) == 1
    row = schedules.get(schedule.id, db=db)
    assert (row.last_status, row.runs, row.running_since) == ("ok", 1, None)
    assert row.noted_at == T0
    assert _wakes(db) == []
    notes = db.execute(
        "SELECT summary FROM context_change_journal "
        "WHERE object_kind='thread_context'"
    ).fetchall()
    assert len(notes) == 1 and "cost = 3.5 usd" in notes[0][0]


@pytest.mark.asyncio
async def test_unbounded_series_never_wakes_or_notes(tmp_path, monkeypatch):
    db = _db()
    _register(db, tmp_path, series=[store.Series("calls", "")])
    _use_run(monkeypatch, _run('{"series":"calls","value":1e9}\n'))
    await _settle_all(db, T0)
    assert _wakes(db) == []
    assert db.execute(
        "SELECT COUNT(*) FROM context_change_journal WHERE object_kind='thread_context'"
    ).fetchone()[0] == 0


@pytest.mark.asyncio
async def test_non_finite_reading_is_an_error_not_a_reading(tmp_path, monkeypatch):
    db = _db()
    schedule = _register(db, tmp_path)
    _use_run(monkeypatch, _run('{"series":"cost","value":NaN}\n'))
    await _settle_all(db, T0)
    assert schedules.get(schedule.id, db=db).last_status == "error"


@pytest.mark.asyncio
async def test_notes_are_rate_limited_to_one_per_hour(tmp_path, monkeypatch):
    db = _db()
    schedule = _register(db, tmp_path, cadence=300)
    _use_run(monkeypatch, _run('{"series":"cost","value":1}\n'))
    await _settle_all(db, T0)
    await _settle_all(db, "2026-01-01T12:05:00+00:00")
    await _settle_all(db, "2026-01-01T13:00:00+00:00")
    count = db.execute(
        "SELECT COUNT(*) FROM context_change_journal WHERE object_kind='thread_context'"
    ).fetchone()[0]
    assert count == 2
    assert schedules.get(schedule.id, db=db).runs == 3


@pytest.mark.asyncio
async def test_breach_wakes_once_per_series_per_day(tmp_path, monkeypatch):
    db = _db()
    _register(db, tmp_path, cadence=300)
    _use_run(monkeypatch, _run('{"series":"cost","value":99}\n'))
    await _settle_all(db, T0)
    await _settle_all(db, "2026-01-01T12:05:00+00:00")
    wakes = _wakes(db)
    assert len(wakes) == 1
    assert "above its max" in wakes[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run", "status"),
    [
        (_run(timed_out=True), "timeout"),
        (_run(code=3, stderr="boom"), "error"),
        (_run("not json\n"), "error"),
        (_run(""), "no_data"),
    ],
)
async def test_failure_states_wake_and_record_status(tmp_path, monkeypatch, run, status):
    db = _db()
    schedule = _register(db, tmp_path)
    _use_run(monkeypatch, run)
    await _settle_all(db, T0)
    row = schedules.get(schedule.id, db=db)
    assert row.last_status == status
    assert row.state == "active"
    assert len(_wakes(db)) >= 1


@pytest.mark.asyncio
async def test_changed_script_file_stops_runs_and_wakes_owner(tmp_path, monkeypatch):
    db = _db()
    schedule = _register(db, tmp_path)
    calls = _use_run(monkeypatch, _run('{"series":"cost","value":1}\n'))
    Path(schedule.script_path).write_text("print('tampered')")
    await _settle_all(db, T0)
    row = schedules.get(schedule.id, db=db)
    assert calls == []  # never executed
    assert (row.state, row.last_status) == ("paused", "hash_mismatch")
    assert len(_wakes(db)) == 1
    assert await _settle_all(db, "2026-01-02T00:00:00+00:00") == 0


@pytest.mark.asyncio
async def test_restart_marks_in_flight_run_interrupted_without_replay(
    tmp_path, monkeypatch,
):
    db = _db()
    schedule = _register(db, tmp_path)
    assert store.claim(schedule, T0, db=db)  # a prior process claimed it, then died
    calls = _use_run(monkeypatch, _run('{"series":"cost","value":1}\n'))
    await _settle_all(db, "2026-01-01T12:00:30+00:00")
    row = schedules.get(schedule.id, db=db)
    assert row.last_status == "interrupted"
    assert row.running_since is None
    assert calls == []
    assert _wakes(db) == []


@pytest.mark.asyncio
async def test_concurrency_cap_limits_started_runs(tmp_path, monkeypatch):
    db = _db()
    for _ in range(4):
        _register(db, tmp_path)
    _use_run(monkeypatch, _run('{"series":"cost","value":1}\n'))
    assert await _settle_all(db, T0) == script_runner.SCRIPT_RUN_CAP
    assert await _settle_all(db, T0) == script_runner.SCRIPT_RUN_CAP


@pytest.mark.asyncio
async def test_concurrency_cap_reclaims_completed_task_before_claiming(tmp_path, monkeypatch):
    db = _db()
    for _ in range(script_runner.SCRIPT_RUN_CAP):
        _register(db, tmp_path)
    completed = asyncio.create_task(asyncio.sleep(0))
    await completed
    script_runner._in_flight["completed"] = completed
    _use_run(monkeypatch, _run('{"series":"cost","value":1}\n'))

    assert await _settle_all(db, T0) == script_runner.SCRIPT_RUN_CAP


@pytest.mark.asyncio
async def test_unavailable_owner_cancels_schedule_without_running(
    tmp_path, monkeypatch,
):
    db = _db()
    schedule = _register(db, tmp_path)
    db.execute(
        "UPDATE threads SET lifecycle_state='purged' WHERE id='owner'"
    )
    calls = _use_run(monkeypatch, _run('{"series":"cost","value":1}\n'))
    assert await _settle_all(db, T0) == 0
    row = schedules.get(schedule.id, db=db)
    assert (row.state, row.last_status) == ("cancelled", "owner_gone")
    assert calls == []


def test_deleting_owner_cancels_its_script_schedules(tmp_path):
    db = _db()
    schedule = _register(db, tmp_path)
    assert schedules.retire_for_thread("owner", T0, db=db) == 1
    assert schedules.get(schedule.id, db=db).state == "cancelled"


# --- pure report + sandbox command ---------------------------------------


def test_classify_uses_only_registered_bounds():
    series = store.Series("x", "", 1.0, 2.0)
    assert [classify(series, v) for v in (0.5, 1.5, 3)] == ["low", "ok", "high"]
    assert classify(store.Series("y", ""), 1e9) == "ok"


def test_oversized_output_is_an_error_not_a_reading(tmp_path):
    db = _db()
    schedule = _register(db, tmp_path)
    big = _run('{"series":"cost","value":1}\n' * 5000)
    assert big.over_cap
    report = build_report(schedule, big)
    assert report.status == "error" and report.readings == []


def test_script_command_caps_stdout_and_keeps_exit_code(tmp_path):
    path = tmp_path / "flood.sh"
    path.write_text("#!/bin/bash\nyes | head -c 10000000\nexit 7\n")
    path.chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", script_command(path)], capture_output=True, timeout=30,
    )
    assert len(result.stdout) == STDOUT_CAP_BYTES + 1
    assert result.returncode != 0

def test_deleting_the_owner_removes_its_schedules_and_the_thread(tmp_path):
    from core.thread_family import delete_thread_family

    db = _db()
    _register(db, tmp_path)
    delete_thread_family(db, ["owner"])
    assert db.execute("SELECT COUNT(*) FROM automation_schedules").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 0


def test_run_settling_after_owner_deletion_is_dropped(tmp_path):
    db = _db()
    schedule = _register(db, tmp_path)
    report = build_report(schedule, _run('{"series":"cost","value":1}\n'))
    db.execute("DELETE FROM automation_schedules")
    assert script_runner.settle(schedule, report, T0, db) is False


@pytest.mark.asyncio
async def test_settlement_failure_records_error_and_clears_claim(tmp_path, monkeypatch):
    db = _db()
    _register(db, tmp_path)
    _use_run(monkeypatch, _run('{"series":"cost","value":1}\n'))

    def fail_settle(*_args, **_kwargs):
        raise RuntimeError("journal write failed")

    monkeypatch.setattr(script_runner, "settle", fail_settle)
    assert await _settle_all(db, T0) == 1
    row = db.execute(
        "SELECT running_since,last_status,last_error FROM automation_schedules"
    ).fetchone()
    assert row["running_since"] is None
    assert row["last_status"] == "error"
    assert "journal write failed" in row["last_error"]


@pytest.mark.integration
def test_tool_registers_script_and_lists_its_status(isolated_db, tmp_path, monkeypatch):
    from infra.database import get_db
    from tools import automations

    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,created_at,updated_at) "
            "VALUES ('owner','user','t','t')"
        )
        db.commit()
    finally:
        db.close()
    path = _script(tmp_path)
    monkeypatch.setattr("tools.sandbox_config.is_path_allowed", lambda *_a, **_k: True)
    actor = {"project_id": None}
    created = automations._operate(
        "owner", actor, "schedule",
        {
            "kind": "script", "script_path": str(path), "cadence_seconds": 600,
            "series": [{"key": "cost", "unit": "usd", "max": 5, "worse": "up"}],
        },
    )["schedule"]
    assert created["kind"] == "script" and created["script_sha256"]
    listed = automations._operate("owner", actor, "list", {})["schedules"]
    assert [item["id"] for item in listed] == [created["id"]]
    assert listed[0]["last_status"] is None
    with pytest.raises(ValueError, match="script_path"):
        automations._operate("owner", actor, "schedule", {"kind": "script"})
    with pytest.raises(ValueError, match="spec"):
        automations._operate("owner", actor, "schedule", {"kind": "cadence"})
    assert automations._operate(
        "owner", actor, "cancel", {"schedule_id": created["id"]},
    ) == {"cancelled": True}


@pytest.mark.asyncio
async def test_run_is_recorded_at_its_finish_time_not_its_start(tmp_path, monkeypatch):
    db = _db()
    schedule = _register(db, tmp_path)
    finish = "2026-01-01T12:04:00+00:00"
    monkeypatch.setattr(script_runner, "finished_at", lambda _started: finish)
    _use_run(monkeypatch, _run('{"series":"cost","value":1}\n'))
    await _settle_all(db, T0)
    row = schedules.get(schedule.id, db=db)
    assert (row.last_run_at, row.noted_at) == (finish, finish)
    assert row.next_run_at > T0  # the claimed slot still came from the start
