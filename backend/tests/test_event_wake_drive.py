"""An event wake starts its idle recipient's Turn once the wake commits."""
from __future__ import annotations

import asyncio

import pytest

import infra.app_loop as app_loop
import infra.thread_execution as thread_execution
from automation import script_runner
from automation.script_readings import RunReport
from events.delivery import enqueue_event_wake, start_idle_after_commit
from infra.database import write_transaction
from tests.test_event_intake import OWNER, _every_rule, _schedule, intake_world  # noqa: F401


@pytest.fixture
def drives(monkeypatch):
    """Run each submitted coroutine at once, recording the threads it drives."""
    driven: list[str] = []

    async def record(thread_id, *args, **kwargs):
        driven.append(thread_id)

    monkeypatch.setattr(thread_execution, "drive_idle_thread", record)
    monkeypatch.setattr(script_runner, "_drive_owner", record)
    monkeypatch.setattr(app_loop, "submit", lambda coroutine: asyncio.run(coroutine))
    return driven


def test_a_rule_wake_from_a_script_event_line_drives_its_idle_owner(intake_world, tmp_path, drives):  # noqa: F811
    _every_rule(intake_world, rule_id="self-wake", destination=OWNER)
    schedule = _schedule(intake_world, tmp_path)

    report = asyncio.run(script_runner._admit_events(
        schedule, RunReport("ok", [], [], events=[{"type": "qa.route", "facts": {"code": "ok"}}], run_id="run-1"),
    ))
    queued = intake_world.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id=? AND delivery_state='queued' "
        "AND json_extract(metadata,'$.event_disposition')='wake'", (OWNER,),
    ).fetchone()[0]

    assert report.status == "ok" and queued == 1
    assert drives == [OWNER]


def test_a_wake_drives_only_after_the_outer_commit_and_never_after_a_rollback(intake_world, drives):  # noqa: F811
    with write_transaction(None, scope="outer") as conn:
        for n in ("1", "2"):
            enqueue_event_wake(OWNER, "qa.route", n, message_id=f"wake-{n}", db=conn)
            start_idle_after_commit(conn, OWNER)
        assert drives == []
    assert drives == [OWNER]

    with pytest.raises(RuntimeError):
        with write_transaction(None, scope="rolled_back") as conn:
            enqueue_event_wake(OWNER, "qa.route", "three", message_id="wake-3", db=conn)
            start_idle_after_commit(conn, OWNER)
            raise RuntimeError("roll back")
    assert drives == [OWNER]
