"""A turn-ending error is `recovered` once the agent's next turn starts past it."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from infra import error_recovery, input_turn_assignment
from infra.database import initialize_schema
from infra.database_migrations import MIGRATIONS
from infra.migration_102_to_103_recovered_errors import migrate_102_to_103
from infra.schema import SCHEMA_VERSION

_ERRORS = ("llm_error", "cc_error", "tool_boundary_violation", "tool_bridge_unavailable")


@pytest.fixture
def db():
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        initialize_schema(connection)
        connection.execute(
            "INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES ('t','u','T','x','x')"
        )
        yield connection


class _Rows:
    def __init__(self, db):
        self.db, self.n = db, 0

    def add(self, role: str, meta: dict | None = None, content: str = "x", seq: bool = True) -> str:
        self.n += 1
        mid = f"m{self.n:02d}"
        self.db.execute(
            "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) VALUES (?,?,?,?,?,?,?)",
            (mid, "t", role, content, json.dumps(meta or {}), f"2026-10-09T00:00:{self.n:02d}",
             self.n if seq else None),
        )
        return mid

    def error(self, kind: str = "llm_error") -> str:
        return self.add("system", {"type": kind}, "boom")

    def work(self) -> str:
        return self.add("assistant", {"llm_tool_calls": [{"id": "c"}]}, "")


def _recovered(db, mid: str):
    return json.loads(db.execute("SELECT metadata FROM messages WHERE id=?", (mid,)).fetchone()[0]).get("recovered")


def _claim(db, **meta) -> list[sqlite3.Row]:
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES ('claimed','t',?,?,?, 'x', NULL)",
        (meta.pop("role", "system"), "input", json.dumps(meta)),
    )
    return db.execute("SELECT * FROM messages WHERE id='claimed'").fetchall()


@pytest.mark.parametrize("kind", _ERRORS)
def test_each_error_type_is_stamped_by_a_turn_that_starts_from_a_wake(db, kind):
    rows = _Rows(db)
    rows.work()
    error = rows.error(kind)
    rows.add("system", {"type": "activity_receipt"}, "Context saved")  # nothing to read in between

    assert len(error_recovery.stamp_at_turn_start(db, "t", _claim(db, type="context_event"))) == 1
    assert _recovered(db, error) is True


def test_a_message_from_the_user_between_the_error_and_the_turn_blocks_the_stamp(db):
    rows = _Rows(db)
    error = rows.error()
    rows.add("user", {}, "try again")

    assert len(error_recovery.stamp_at_turn_start(db, "t", _claim(db, type="context_event"))) == 0
    assert _recovered(db, error) is None


def test_a_user_message_that_starts_the_turn_blocks_the_stamp(db):
    rows = _Rows(db)
    error = rows.error()

    assert len(error_recovery.stamp_at_turn_start(db, "t", _claim(db, role="user"))) == 0
    assert _recovered(db, error) is None


def test_a_message_from_another_agent_does_not_block_the_stamp(db):
    rows = _Rows(db)
    error = rows.error()
    rows.add("user", {"sender_kind": "agent"}, "ping")

    assert len(error_recovery.stamp_at_turn_start(db, "t", _claim(db, type="context_event"))) == 1
    assert _recovered(db, error) is True


def test_an_update_for_the_user_or_another_notice_blocks_the_stamp(db):
    rows = _Rows(db)
    first = rows.error()
    rows.add("assistant", {"for_user": True}, "done")
    second = rows.error("cc_error")
    rows.add("system", {"type": "sandbox_denial"}, "denied")

    assert len(error_recovery.stamp_at_turn_start(db, "t", _claim(db, type="context_event"))) == 0
    assert (_recovered(db, first), _recovered(db, second)) == (None, None)


def test_an_old_error_before_a_user_message_stays_unstamped_after_many_later_turns(db):
    rows = _Rows(db)
    old = rows.error()
    rows.add("user", {}, "read it")
    for _ in range(20):
        rows.work()
        assert len(error_recovery.stamp_at_turn_start(db, "t", _claim(db, type="context_event"))) == 0
        db.execute("DELETE FROM messages WHERE id='claimed'")
    recent = rows.error("cc_error")

    assert len(error_recovery.stamp_at_turn_start(db, "t", _claim(db, type="context_event"))) == 1
    assert (_recovered(db, old), _recovered(db, recent)) == (None, True)


def test_consecutive_errors_are_stamped_together_and_an_earlier_one_stays_when_read_between(db):
    rows = _Rows(db)
    read = rows.error()
    rows.add("user", {}, "hello")
    first, second = rows.error(), rows.error("cc_error")

    assert len(error_recovery.stamp_at_turn_start(db, "t", _claim(db, type="context_event"))) == 2
    assert (_recovered(db, read), _recovered(db, first), _recovered(db, second)) == (None, True, True)


def test_a_stamped_row_is_stamped_once_and_other_threads_are_untouched(db):
    rows = _Rows(db)
    error = rows.error()
    db.execute("INSERT INTO threads (id,user_id,title,created_at,updated_at) VALUES ('o','u','O','x','x')")
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES ('other-error','o','system','boom','{\"type\":\"llm_error\"}','x',1)"
    )
    claimed = _claim(db, type="context_event")

    assert len(error_recovery.stamp_at_turn_start(db, "t", claimed)) == 1
    assert len(error_recovery.stamp_at_turn_start(db, "t", claimed)) == 0
    assert _recovered(db, error) is True and _recovered(db, "other-error") is None


def test_the_claim_that_starts_a_turn_writes_the_stamp(db, monkeypatch):
    rows = _Rows(db)
    error = rows.error()
    claimed = _claim(db, type="context_event")
    monkeypatch.setattr(input_turn_assignment, "stamp_initial_claim", lambda *_a: None)
    monkeypatch.setattr(
        input_turn_assignment, "without_login_sql", lambda: "COALESCE(metadata, '{}')",
    )
    monkeypatch.setattr("plans.public.thread_paused_since", lambda *_a: None)

    input_turn_assignment.assign_turn(db, "t", "turn-1", claimed, initial=True)
    assert _recovered(db, error) is True

    other = rows.error()
    input_turn_assignment.assign_turn(db, "t", "turn-1", claimed)  # a mid-turn claim is not a start
    assert _recovered(db, other) is None


def _error_then_seed(db) -> tuple[_Rows, str]:
    rows = _Rows(db)
    rows.work()
    error = rows.error()
    rows.add("system", {"cc": True, "type": "seed"}, "starting context")  # never drawn: nothing to read
    return rows, error


def test_a_seed_between_an_error_and_the_next_turn_does_not_hold_it_open(db):
    _, error = _error_then_seed(db)

    assert error_recovery.stamp_at_turn_start(db, "t", _claim(db, type="context_event")) == [error]
    assert _recovered(db, error) is True


def test_the_backfill_gives_the_same_answer_for_a_seed_after_an_error(db):
    rows, error = _error_then_seed(db)
    rows.work()  # the turn that started past it

    assert error_recovery.backfill(db) == 1
    assert _recovered(db, error) is True


def _backfill_history(db) -> dict[str, str]:
    rows = _Rows(db)
    ids = {}
    rows.add("user", {}, "go")
    ids["then_work"] = rows.error()
    rows.add("system", {"type": "context_event"}, "wake")
    rows.work()
    ids["then_user"] = rows.error("cc_error")
    rows.add("user", {}, "retry")
    rows.work()
    ids["then_nothing"] = rows.error("tool_bridge_unavailable")
    ids["then_said"] = rows.error("tool_boundary_violation")
    rows.add("system", {"type": "subagent_done"}, "child finished")
    rows.work()
    ids["already"] = rows.add("system", {"type": "llm_error", "recovered": True}, "boom")
    ids["last"] = rows.error()
    return ids


def test_the_backfill_applies_the_same_rule_to_existing_rows(db):
    ids = _backfill_history(db)

    assert error_recovery.backfill(db) == 3
    assert {name: _recovered(db, mid) for name, mid in ids.items()} == {
        "then_work": True, "then_user": None, "then_nothing": True,
        "then_said": True, "already": True, "last": None,
    }
    assert error_recovery.backfill(db) == 0


def test_the_backfill_keeps_an_error_a_reply_for_the_user_follows(db):
    rows = _Rows(db)
    rows.add("user", {}, "go")
    error = rows.error()
    rows.add("assistant", {"for_user": True}, "here is what happened")
    rows.work()

    assert error_recovery.backfill(db) == 0
    assert _recovered(db, error) is None


def test_the_migration_is_registered_and_runs_the_backfill(db):
    assert SCHEMA_VERSION == 162 and MIGRATIONS[102] is migrate_102_to_103
    ids = _backfill_history(db)

    migrate_102_to_103(db)

    assert _recovered(db, ids["then_work"]) is True and _recovered(db, ids["last"]) is None
