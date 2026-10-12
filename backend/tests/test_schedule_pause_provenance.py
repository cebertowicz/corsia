"""A schedule retains whether its creating Turn carried the user's word."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from core.message_history import render_agent_envelope
from infra import state
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_SQL, SCHEMA_VERSION
from tools import automations
from plans.pause_authority import (
    is_users_word, reaches_paused_agent, turn_carries_users_word,
)


@pytest.mark.parametrize(
    ("role", "metadata", "expected"),
    [
        ("user", {}, True),
        ("user", {"sender_kind": "agent"}, False),
        ("user", {"sender_kind": "eval"}, False),
        ("user", {"completion": True}, False),
        ("user", {"routed_event": "x"}, False),
        ("user", {"sender_kind": "system"}, False),
        ("system", {"event_type": "plan.decision_answered"}, True),
        ("system", {"event_type": "automation.scheduled_wake"}, False),
    ],
)
def test_is_users_word_uses_the_canonical_non_human_markers(role, metadata, expected):
    assert is_users_word({"role": role, "metadata": json.dumps(metadata)}) is expected


def test_fresh_schema_records_schedule_provenance_as_false():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)

    columns = {
        row["name"]: row
        for row in db.execute("PRAGMA table_info(automation_schedules)")
    }

    assert columns["asked_by_user"]["notnull"] == 1
    assert columns["asked_by_user"]["dflt_value"] == "0"


def test_scheduled_wake_without_its_provenance_is_held():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    row = {
        "thread_id": "owner",
        "role": "system",
        "metadata": json.dumps({
            "event_type": "automation.scheduled_wake", "subject_id": "missing",
        }),
    }

    assert not reaches_paused_agent(row, db=db)


@pytest.mark.parametrize("asked_by_user", [0, 1])
def test_scheduled_wake_reaches_a_paused_agent_only_when_the_user_asked(asked_by_user):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('owner','user','test/model','t','t')"
    )
    db.execute(
        "INSERT INTO automation_schedules "
        "(id,thread_id,scope_id,kind,spec,next_run_at,max_runs,state,created_by_actor,"
        "asked_by_user,created_at,updated_at) VALUES "
        "('schedule','owner','scope','clock','2030-01-01T00:00:00+00:00',"
        "'2030-01-01T00:00:00+00:00',1,'active','owner',?,'t','t')",
        (asked_by_user,),
    )
    row = {
        "thread_id": "owner",
        "role": "system",
        "metadata": json.dumps({
            "event_type": "automation.scheduled_wake", "subject_id": "schedule",
        }),
    }

    assert reaches_paused_agent(row, db=db) is bool(asked_by_user)


def test_turn_carries_only_a_human_input_as_the_users_word():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('owner','user','test/model','t','t')"
    )
    db.execute(
        "INSERT INTO messages "
        "(id,thread_id,role,content,metadata,delivery_state,created_at,active_turn_id) "
        "VALUES ('peer','owner','user','text','{\"sender_kind\": \"agent\"}',"
        "'delivered','t','turn-1')"
    )
    db.execute(
        "INSERT INTO messages "
        "(id,thread_id,role,content,metadata,delivery_state,created_at,active_turn_id) "
        "VALUES ('user','owner','user','text','{}','delivered','t','turn-2')"
    )

    assert not turn_carries_users_word(db, "turn-1")
    assert turn_carries_users_word(db, "turn-2")


@pytest.mark.asyncio
async def test_schedule_tool_reads_the_native_turn_identity(monkeypatch):
    captured = {}

    def operate(*_args, turn_id=None, **_kwargs):
        captured["turn_id"] = turn_id
        return {"schedule": {}}

    async def run_in_db(operation):
        return operation()

    monkeypatch.setattr(automations, "_require_context", lambda _kwargs: ("owner", {}))
    monkeypatch.setattr(automations, "_operate", operate)
    monkeypatch.setattr(automations, "run_in_db", run_in_db)
    token = state._current_turn_identity.set(SimpleNamespace(turn_id="native-turn"))
    try:
        await automations._execute({"kind": "cadence"}, "schedule")
    finally:
        state._current_turn_identity.reset(token)

    assert captured["turn_id"] == "native-turn"


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["claude-code", "codex"])
async def test_harness_bridges_bind_the_turn_identity_for_schedule_tools(monkeypatch, harness):
    from subagents import harness_tool_bridge

    captured = {}

    class Registry:
        async def execute(self, _name, _arguments):
            captured["turn_id"] = state._current_turn_identity.get().turn_id
            return "ok"

        def get(self, _name):
            return None

    async def publish(*_args, **_kwargs):
        return "published"

    monkeypatch.setattr(harness_tool_bridge, "publish_completed_tool_result", publish)
    identity = SimpleNamespace(turn_id=f"{harness}-turn")

    assert await harness_tool_bridge.execute_tool(
        Registry(), thread_id="owner", turn_identity=identity,
        name="automation_schedule", arguments={}, harness=harness,
    ) == "published"
    assert captured["turn_id"] == identity.turn_id


def test_paused_scheduled_wake_uses_the_existing_opening_with_a_stop_clause():
    rendered = render_agent_envelope(
        "scheduled task",
        {
            "plan_paused_since": "2030-01-01T00:00:00+00:00",
            "event_type": "automation.scheduled_wake",
        },
    )

    assert "Do this schedule's task only, then stop." in rendered.managed_prefix


def test_migration_113_to_114_defaults_legacy_schedules_to_false():
    db = sqlite3.connect(":memory:")
    legacy_schema = SCHEMA_SQL.replace(
        "        asked_by_user INTEGER NOT NULL DEFAULT 0,\n", "",
    )
    db.executescript(legacy_schema)
    db.execute("PRAGMA user_version=113")

    migrated = migrate_to_current(db, SCHEMA_VERSION)

    assert (migrated.from_version, migrated.to_version) == (113, 114)
    columns = {
        row[1]: row
        for row in db.execute("PRAGMA table_info(automation_schedules)")
    }
    assert columns["asked_by_user"][4] == "0"
