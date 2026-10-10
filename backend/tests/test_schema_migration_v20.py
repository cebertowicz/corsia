"""Stored plan inputs take the one context-event shape plan_wake writes."""

from contextlib import closing
import json
import sqlite3

import pytest

from infra.database import initialize_schema
from infra.database_migrations import migrate_to_current
from core.message_history import is_system_context_event

WAKE_CONTENT = "[CORSIA plan] The work delegated for E4 has returned and awaits your verdict.\n{\"next\": {}}"
ANSWER_CONTENT = "[PLAN DECISION ANSWERED]\nDecision d1: ship it."


@pytest.fixture
def legacy_database(tmp_path):
    path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute("INSERT INTO threads(id,user_id,title,created_at,updated_at) VALUES('t','owner','Thread','t','t')")
        rows = [
            ("plan-wake:child_returned:n1:3", "user", WAKE_CONTENT,
             {"event_type": "plan.child_returned", "origin": "plan", "plan_id": "p1", "node_id": "n1"}),
            ("plan-decision-answer:d1", "user", ANSWER_CONTENT,
             {"event_type": "plan.decision_answered", "origin": "plan", "plan_id": "p1", "decision_id": "d1"}),
            ("plan-event:e1", "system", "Step E4 accepted",
             {"type": "activity_receipt", "event_type": "plan.node_accepted", "origin": "plan", "plan_id": "p1"}),
            ("usage-defect:1", "system", "Usage recording failed",
             {"type": "activity_receipt", "event_type": "usage.recording_failed", "origin": "plan", "plan_node_id": "n1"}),
            ("assistant:1", "assistant", "On it.", {"origin": "plan", "plan_node_id": "n1"}),
            ("user:1", "user", "please ship it", None),
        ]
        for message_id, role, content, metadata in rows:
            db.execute(
                "INSERT INTO messages(id,thread_id,role,content,created_at,delivery_state,metadata)"
                " VALUES(?,?,?,?,?,?,?)",
                (message_id, "t", role, content, "t", "delivered",
                 None if metadata is None else json.dumps(metadata)),
            )
        db.execute("PRAGMA user_version=19")
        db.commit()
    return path


def _rows(db):
    return {
        row[0]: {"role": row[1], "content": row[2], "metadata": json.loads(row[3]) if row[3] else None}
        for row in db.execute("SELECT id, role, content, metadata FROM messages ORDER BY rowid")
    }


def test_stored_plan_inputs_become_system_context_events_and_nothing_else_moves(legacy_database):
    with closing(sqlite3.connect(legacy_database)) as db:
        before = _rows(db)
        result = migrate_to_current(db, 20)
        assert result.migrated and result.to_version == 20
        after = _rows(db)

    for message_id, summary in (
        ("plan-wake:child_returned:n1:3", WAKE_CONTENT.split("\n", 1)[0]),
        ("plan-decision-answer:d1", "[PLAN DECISION ANSWERED]"),
    ):
        row = after[message_id]
        assert row["role"] == "system"
        assert row["content"] == before[message_id]["content"]
        assert row["metadata"] == {
            **before[message_id]["metadata"],
            "type": "context_event", "event_disposition": "wake", "summary": summary,
        }
        assert is_system_context_event({"role": row["role"], "metadata": json.dumps(row["metadata"])})

    for message_id in ("plan-event:e1", "usage-defect:1", "assistant:1", "user:1"):
        assert after[message_id] == before[message_id]


def test_a_current_database_is_left_alone(legacy_database):
    with closing(sqlite3.connect(legacy_database)) as db:
        migrate_to_current(db, 20)
        rows = _rows(db)
        assert not migrate_to_current(db, 20).migrated
        assert _rows(db) == rows
