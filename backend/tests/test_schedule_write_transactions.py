"""Schedule mutations own their writes without settling another caller's work."""

from contextlib import closing
import json
import sqlite3

import pytest

from automation import schedules
from infra import database
from tools import automations

NOW = "2030-01-01T00:00:00+00:00"


@pytest.fixture
def schedule_scope(isolated_db):
    with database.write_transaction(None, scope="schedule_transaction_seed") as db:
        db.execute(
            "INSERT INTO projects (id,name,created_at,updated_at) "
            "VALUES ('schedule-project','Original','t','t')",
        )
        for thread in ("schedule-thread", "other-thread"):
            db.execute(
                "INSERT INTO threads (id,user_id,project_id,created_at,updated_at) "
                "VALUES (?,'fixture-user','schedule-project','t','t')", (thread,),
            )
        db.execute(
            "INSERT INTO automation_schedules "
            "(id,thread_id,scope_id,kind,spec,next_run_at,max_runs,state,"
            "created_by_actor,created_at,updated_at) VALUES "
            "('existing','schedule-thread','schedule-project','cadence','60',"
            "'2030-01-01T00:01:00+00:00',2,'active','schedule-thread',?,?)",
            (NOW, NOW),
        )
    return isolated_db


def _snapshot(path):
    with closing(sqlite3.connect(str(path))) as db:
        return (
            db.execute("SELECT name FROM projects WHERE id='schedule-project'").fetchone()[0],
            db.execute("SELECT id,state FROM automation_schedules ORDER BY id").fetchall(),
        )


def _writer_free(path):
    with closing(sqlite3.connect(str(path), timeout=0)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.rollback()


def _mutate(operation, *, db):
    if operation == "create":
        return schedules.create(
            "schedule-thread", "schedule-project", "schedule-thread",
            kind="cadence", spec="60", max_runs=2, now=NOW, db=db,
        )
    return schedules.cancel(
        "existing", thread_id="schedule-thread", actor_id="schedule-thread", db=db,
    )


@pytest.mark.parametrize("operation", ["create", "cancel"])
def test_named_schedule_mutation_leaves_commit_to_the_caller(schedule_scope, operation):
    db = database.get_db()
    try:
        db.execute("UPDATE projects SET name='Caller' WHERE id='schedule-project'")
        _mutate(operation, db=db)
        assert db.in_transaction
        assert _snapshot(schedule_scope) == ("Original", [("existing", "active")])
        db.rollback()
        assert _snapshot(schedule_scope) == ("Original", [("existing", "active")])
    finally:
        db.close()


@pytest.mark.parametrize("operation", ["schedule", "cancel", "list", "invalid"])
def test_tool_operation_never_settles_a_foreign_pooled_write(schedule_scope, operation):
    db = database.get_db()
    params = {"kind": "cadence", "spec": "60", "max_runs": 2, "schedule_id": "existing"}
    if operation == "invalid":
        params["max_runs"] = 0
    try:
        db.execute("UPDATE projects SET name='Caller' WHERE id='schedule-project'")
        try:
            automations._operate(
                "schedule-thread", {"project_id": "schedule-project"},
                "schedule" if operation == "invalid" else operation, params,
            )
        except (database.TransactionOwnershipError, ValueError):
            pass  # Refusal must not commit or discard this caller's pending work.
        assert db.in_transaction
        assert db.execute("SELECT name FROM projects WHERE id='schedule-project'").fetchone()[0] == "Caller"
        assert _snapshot(schedule_scope) == ("Original", [("existing", "active")])
        db.rollback()
    finally:
        db.close()


@pytest.mark.parametrize("operation", ["create", "cancel"])
def test_partial_schedule_failure_cannot_leak_into_a_later_caller_commit(schedule_scope, operation):
    event = "INSERT" if operation == "create" else "UPDATE"
    with database.write_transaction(None, scope="schedule_fault_seed") as db:
        db.execute(
            f"CREATE TRIGGER reject_schedule AFTER {event} ON automation_schedules "
            "BEGIN SELECT RAISE(FAIL, 'schedule rejected'); END",
        )
    db = database.get_db()
    try:
        db.execute("UPDATE projects SET name='Caller' WHERE id='schedule-project'")
        with pytest.raises(sqlite3.IntegrityError, match="schedule rejected"):
            _mutate(operation, db=db)
        assert db.in_transaction
        db.commit()
        assert _snapshot(schedule_scope) == ("Caller", [("existing", "active")])
    finally:
        db.close()


@pytest.mark.parametrize("operation", ["schedule", "cancel"])
def test_standalone_commit_failure_is_loud_and_leaves_no_schedule_change(
    schedule_scope, monkeypatch, operation,
):
    class FailedCommit(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError("schedule commit rejected")

    db = sqlite3.connect(str(schedule_scope), factory=FailedCommit)
    db.row_factory = sqlite3.Row
    monkeypatch.setattr(database, "get_db", lambda: db)
    monkeypatch.setattr(automations, "get_db", lambda: db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="schedule commit rejected"):
            automations._operate(
                "schedule-thread", {"project_id": "schedule-project"}, operation,
                {"kind": "cadence", "spec": "60", "max_runs": 2, "schedule_id": "existing"},
            )
        assert _snapshot(schedule_scope) == ("Original", [("existing", "active")])
        _writer_free(schedule_scope)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_real_tools_commit_bounded_schedule_and_preserve_actor_visibility(schedule_scope):
    result = json.loads(await automations.AutomationScheduleTool().execute(
        _thread_id="schedule-thread", kind="cadence", spec="60", max_runs=2,
        payload={"note": "later"},
    ))
    created = result["schedule"]
    assert result["notice"] == (
        "Held while your plan is paused: not created in a Turn with the user's message."
    )
    assert len(result["notice"].encode()) < 90
    assert (created["thread_id"], created["scope_id"], created["created_by_actor"]) == (
        "schedule-thread", "schedule-project", "schedule-thread",
    )
    assert (created["state"], created["runs"], created["max_runs"], created["payload"]) == (
        "active", 0, 2, {"note": "later"},
    )
    assert "asked_by_user" not in created
    assert (created["id"], "active") in _snapshot(schedule_scope)[1]
    assert database.get_db().execute(
        "SELECT asked_by_user FROM automation_schedules WHERE id=?", (created["id"],),
    ).fetchone()[0] == 0
    _writer_free(schedule_scope)
    foreign = json.loads(await automations.AutomationListTool().execute(_thread_id="other-thread"))
    assert foreign == {"schedules": []}
    refused = json.loads(await automations.AutomationCancelTool().execute(
        _thread_id="other-thread", schedule_id=created["id"],
    ))
    assert refused == {"cancelled": False}
    cancelled = json.loads(await automations.AutomationCancelTool().execute(
        _thread_id="schedule-thread", schedule_id=created["id"],
    ))
    assert cancelled == {"cancelled": True}
    assert (created["id"], "cancelled") in _snapshot(schedule_scope)[1]
    _writer_free(schedule_scope)


def test_schedule_created_from_a_users_turn_records_provenance(schedule_scope):
    db = database.get_db()
    try:
        from infra import queries

        queries.insert_message("schedule-thread", "user", "schedule this", {}, "user-input", db=db)
        db.execute("UPDATE messages SET active_turn_id='turn-1' WHERE id='user-input'")
        db.commit()
        result = automations._operate(
            "schedule-thread", {"project_id": "schedule-project"}, "schedule",
            {"kind": "cadence", "spec": "60", "max_runs": 2}, turn_id="turn-1",
        )

        assert result["notice"] == (
            "Runs while your plan is paused: created in a Turn with the user's message."
        )
        assert len(result["notice"].encode()) < 90
        assert "asked_by_user" not in result["schedule"]
        assert db.execute(
            "SELECT asked_by_user FROM automation_schedules WHERE id=?",
            (result["schedule"]["id"],),
        ).fetchone()[0] == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_invalid_schedule_keeps_the_existing_tool_error_and_writes_nothing(schedule_scope):
    result = await automations.AutomationScheduleTool().execute(
        _thread_id="schedule-thread", kind="clock", spec="2030-01-01T00:00:00+00:00", max_runs=2,
    )
    assert result == "Error: clock schedules require max_runs=1"
    assert _snapshot(schedule_scope) == ("Original", [("existing", "active")])
    _writer_free(schedule_scope)
