"""A rule-started script run the backend loses before it settles reaches its rule's owner.

The real intake, evaluation, claim sweep and wake send. Only the sandboxed process is
faked, and a restart is the runner losing its tasks: the next process holds none."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

import infra.app_loop as app_loop
import infra.thread_execution as thread_execution
from automation import script_rule_runs, script_runner
from automation.script_sandbox import ScriptRun
from events import rule_runs
from infra.database import get_db
from tests.test_event_rules_run_script import (  # noqa: F401 - the run-script world and its helpers
    OWNER, _emit, _outcomes, _rule, _script, _sweep, _write, runs, world,
)


@pytest.fixture
def cut(runs, monkeypatch):
    """The run-script world, where a script named in ``hanging`` never returns and each idle
    drive a committed wake asks for is recorded."""
    hanging: set[str] = set()
    inner = script_runner.run_script

    async def run(thread_id, project_id, path, **kwargs):
        if path.name in hanging:
            runs["calls"].append((path.name, json.loads(kwargs["stdin"])))
            await asyncio.Event().wait()
        return await inner(thread_id, project_id, path, **kwargs)

    async def record(thread_id, *args, **kwargs):
        runs["idle_drives"].append(thread_id)

    runs["hanging"], runs["idle_drives"] = hanging, []
    monkeypatch.setattr(script_runner, "run_script", run)
    monkeypatch.setattr(thread_execution, "drive_idle_thread", record)
    # The claim commits on a database thread, off the test's loop, so the drive runs there.
    monkeypatch.setattr(app_loop, "submit", lambda coroutine: asyncio.run(coroutine))
    return runs


def _lose_the_runs_in_flight() -> int:
    """Start what is queued, then lose the process under it; return how many runs were lost."""

    async def go() -> int:
        started = await script_runner.drive(datetime.now(timezone.utc).isoformat())
        await asyncio.sleep(0.05)  # each run reaches its script
        tasks = list(script_runner._in_flight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        script_runner._in_flight.clear()
        script_runner._holds.clear()
        return started

    return asyncio.run(go())


def _wakes(event: str = script_rule_runs.INTERRUPTED_EVENT) -> list[tuple[str, dict]]:
    """Each wake of ``event`` the owner was sent: its summary line and its details."""
    db = get_db()
    try:
        rows = db.execute(
            "SELECT content FROM messages WHERE thread_id=? AND json_extract(metadata,'$.event_type')=? "
            "AND json_extract(metadata,'$.event_disposition')='wake' ORDER BY rowid", (OWNER, event)).fetchall()
    finally:
        db.close()
    wakes = []
    for row in rows:
        head, details = row["content"].split("\nDetails:\n", 1)
        wakes.append((head.split("\nSummary: ", 1)[1], json.loads(details)))
    return wakes


def _person_lines() -> list[str]:
    """The line the person sees in the chat for each interrupted-run wake."""
    db = get_db()
    try:
        return [row[0] for row in db.execute(
            "SELECT json_extract(metadata,'$.summary') FROM messages WHERE thread_id=? "
            "AND json_extract(metadata,'$.event_type')=? ORDER BY rowid",
            (OWNER, script_rule_runs.INTERRUPTED_EVENT)).fetchall()]
    finally:
        db.close()


def test_a_run_cut_by_a_restart_fails_and_wakes_its_rules_owner_once_naming_it(cut):
    script_id = _script(cut, "cut.sh")
    rule = _rule(script_id)
    cut["hanging"].add("cut.sh")
    event_id = _emit()

    assert _lose_the_runs_in_flight() == 1 and _outcomes(rule["id"]) == [("running", None)]
    assert _wakes() == []
    _sweep()  # the next process's first sweep

    assert _outcomes(rule["id"]) == [("failed", rule_runs.INTERRUPTED)]
    [(summary, details)] = _wakes()
    assert summary == "A restart stopped a script run your event rule started. It is not replayed."
    assert _person_lines() == ["Script run stopped when CORSIA restarted"]  # the agent's line is not the person's
    assert details["runs"] == [{"rule_id": rule["id"], "event_id": event_id, "event_type": "qa.route"}]
    assert "event_rules explain" in details["next"]
    assert cut["idle_drives"] == [OWNER]

    _sweep()  # nothing replays it and nothing wakes twice
    assert [name for name, _ in cut["calls"]] == ["cut.sh"] and len(_wakes()) == 1
    assert cut["idle_drives"] == [OWNER]
    db = get_db()
    try:
        row = db.execute("SELECT last_status, running_since FROM automation_schedules WHERE id=?",
                         (script_id,)).fetchone()
    finally:
        db.close()
    assert (row["last_status"], row["running_since"]) == ("interrupted", None)


def test_two_runs_cut_by_one_restart_reach_their_owner_as_one_wake_with_each_runs_item(cut):
    first, second = _rule(_script(cut, "one.sh")), _rule(_script(cut, "two.sh"))
    cut["hanging"].update({"one.sh", "two.sh"})
    event_id = _emit()
    # The item a connector's event names is stored on the row at evaluation; this event has none.
    _write("UPDATE event_route_decisions SET artifact_integration='qa', artifact_type='thing', "
           "artifact_external_id='shelf/box#7' WHERE rule_id=?", first["id"])

    assert _lose_the_runs_in_flight() == 2
    _sweep()

    [(summary, details)] = _wakes()
    assert summary == "A restart stopped 2 script runs your event rules started. They are not replayed."
    assert _person_lines() == ["2 script runs stopped when CORSIA restarted"]
    assert details["runs"] == [
        {"rule_id": first["id"], "event_id": event_id, "event_type": "qa.route", "item": "shelf/box#7"},
        {"rule_id": second["id"], "event_id": event_id, "event_type": "qa.route"},
    ]
    assert cut["idle_drives"] == [OWNER]


def test_a_cut_run_still_wakes_on_a_day_its_script_already_failed(cut):
    rule = _rule(_script(cut, "twice.sh"))
    cut["outputs"]["twice.sh"] = ScriptRun(3, b"", b"boom", False)
    _emit(code="first")
    _sweep()
    assert cut["driven"] == [OWNER] and len(_wakes("script.error")) == 1  # the day's failure wake is spent

    cut["hanging"].add("twice.sh")
    event_id = _emit(code="second")
    assert _lose_the_runs_in_flight() == 1
    _sweep()

    assert [outcome for outcome, _ in _outcomes(rule["id"])] == ["failed", "failed"]
    [(_, details)] = _wakes()
    assert [run["event_id"] for run in details["runs"]] == [event_id]


def test_a_run_that_was_only_skipped_wakes_nobody(cut, monkeypatch):
    rule = _rule(_script(cut, "paused.sh"))
    _emit()
    monkeypatch.setattr("plans.public.thread_paused_since", lambda _db, _thread: "2026-01-01T00:00:00+00:00")
    _sweep()

    assert _outcomes(rule["id"]) == [("skipped", rule_runs.OWNER_PAUSED)]
    db = get_db()
    try:
        sent = db.execute("SELECT COUNT(*) FROM messages WHERE thread_id=?", (OWNER,)).fetchone()[0]
    finally:
        db.close()
    assert sent == 0


def test_a_cut_run_whose_owner_is_gone_is_parked_and_the_sweep_goes_on(cut):
    rule = _rule(_script(cut, "orphan.sh"))
    cut["hanging"].add("orphan.sh")
    event_id = _emit()
    assert _lose_the_runs_in_flight() == 1
    _write("UPDATE threads SET lifecycle_state='archived', archived_reason='user' WHERE id=?", OWNER)
    _sweep()

    assert _outcomes(rule["id"]) == [("failed", rule_runs.INTERRUPTED)]
    db = get_db()
    try:
        [parked] = db.execute("SELECT internal_event, reason, payload_json FROM dead_letter_events").fetchall()
    finally:
        db.close()
    assert (parked["internal_event"], parked["reason"]) == (script_rule_runs.INTERRUPTED_EVENT, "owner_inactive")
    payload = json.loads(parked["payload_json"])
    assert payload["owner"] == OWNER and [run["event_id"] for run in payload["runs"]] == [event_id]
    assert _wakes() == [] and cut["idle_drives"] == []
