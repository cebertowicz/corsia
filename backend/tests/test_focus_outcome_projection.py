"""Passive previews and human attention through the canonical query and seen owners."""
import re
from datetime import datetime

import pytest

from infra import presence, presence_records, thread_updates
from plans import operator_chat_state
from tests.plan_close_next_helpers import HUMAN
from tests.test_thread_updates import db, _message, _node, _plan_update  # noqa: F401 - fixture
from tests.plan_writer_authorizer import raw_plan_seed


def state(db):
    return operator_chat_state.read_thread_states(["owner"], actor=HUMAN, db=db)["states"]["owner"]


def visit(db):
    presence_records.enter(db, "user", "owner", datetime(2026, 9, 29, 8))
    presence_records.mark_exit_pending(db, "user", "owner", datetime(2026, 9, 29, 9), "navigated", datetime(2026, 9, 29, 9))
    presence_records.finalize(db, presence_records.row(db, "user", "owner"), 1, datetime(2026, 9, 29, 9), "chat")


@pytest.mark.parametrize(("kind", "preview"), [
    ("done", "Completed: Original title"),
    ("done_follow_up", "Completed: Original title · Follow-up added"),
    ("closed", "Closed: Original title"),
])
def test_passive_only_history_previews_without_a_dot_through_reload_and_manual_seen(db, kind, preview):
    node = _node(db, title="Original title")
    _plan_update(db, "outcome", node, kind=kind, at="2026-09-29T10:00:00")
    db.execute("UPDATE plan_nodes SET title='Renamed' WHERE id=?", (node,))
    with raw_plan_seed(db):
        db.execute("DELETE FROM plan_nodes WHERE id=?", (node,))
    for _ in range(2):  # same canonical read used by nudge reload and initial load
        assert state(db)["latest_update"]["summary"] == preview
        assert state(db)["has_update"] is False
    visit(db)
    assert presence.mark_seen(db, user_id="user", thread_id="owner", unread=True, now=datetime(2026, 9, 29, 11))
    assert state(db)["has_update"] is True
    assert presence.mark_seen(db, user_id="user", thread_id="owner", unread=False, now=datetime(2026, 9, 29, 12))
    assert state(db)["has_update"] is False
    _plan_update(db, "later-passive", _node(db), kind=kind, at="2026-09-29T13:00:00")
    assert state(db)["has_update"] is False


@pytest.mark.parametrize(("kind", "node_state", "preview"), [
    ("done", "accepted", "Completed: Ship the export"),
    ("closed", "closed", "Closed: Ship the export"),
])
def test_sidebar_keeps_outcome_for_a_receipted_person_step(db, kind, node_state, preview):
    node = _node(db)
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET assignee='user', state=? WHERE id=?", (node_state, node))
    _plan_update(db, "person-step", node, kind="person_step", at="2026-09-29T10:00:00")
    _plan_update(db, "outcome", node, kind=kind, at="2026-09-29T11:00:00")
    cards, _ = thread_updates.read_newest_cards(db, "owner", min_seq=0)
    assert next(card for card in cards if card["kind"] == "person_step")["step_state"] == ("done" if kind == "done" else "closed")
    assert state(db)["latest_update"]["summary"] == preview


def test_new_passive_preview_preserves_older_unread_marked_until_active_visit_then_exit(db):
    visit(db)
    msg = _message(db, "owner", 1)
    db.execute("UPDATE messages SET content='**Decision needed**' WHERE id=?", (msg,))
    thread_updates.record_marked(db, thread_id="owner", message_id=msg, node_id=None, created_at="2026-09-29T10:00:00")
    _plan_update(db, "newer", _node(db), at="2026-09-29T11:00:00")
    assert state(db)["latest_update"]["summary"] == "Completed: Ship the export"
    assert state(db)["has_update"] is True
    presence_records.enter(db, "user", "owner", datetime(2026, 9, 29, 12))
    assert state(db)["has_update"] is False
    presence_records.mark_exit_pending(db, "user", "owner", datetime(2026, 9, 29, 13), "navigated", datetime(2026, 9, 29, 13))
    presence_records.finalize(db, presence_records.row(db, "user", "owner"), 1, datetime(2026, 9, 29, 13), "chat")
    _plan_update(db, "later", _node(db), at="2026-09-29T14:00:00")
    assert state(db)["has_update"] is False


def test_equal_timestamp_uses_independent_stable_id_order_for_display_and_attention(db):
    node = _node(db)
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET assignee='user', state='failed' WHERE id=?", (node,))  # only a user-owned failure is attention
    _plan_update(db, "failed-first", node, kind="failed")
    _plan_update(db, "passive-last", node, kind="closed")
    display, attention = thread_updates.latest_by_thread_with_attention(db, ["owner"])
    assert display["owner"]["summary"] == "Closed: Ship the export"
    assert attention["owner"]["summary"] == "Failed: Ship the export"
    assert display["owner"]["id"] > attention["owner"]["id"]
    assert state(db)["has_update"] is True  # failure/block dots deliberately remain eligible


@pytest.mark.parametrize("content", ["", "   ", "```text\ncode only\n```"])
def test_blank_marked_preview_remains_empty_and_attention_eligible(db, content):
    msg = _message(db, "owner", 1)
    db.execute("UPDATE messages SET content=? WHERE id=?", (content, msg))
    thread_updates.record_marked(db, thread_id="owner", message_id=msg, node_id=None, created_at="2026-09-29T10:00:00")
    assert state(db)["latest_update"]["summary"] is None
    assert state(db)["has_update"] is True


def test_actual_dual_projection_is_one_bounded_indexed_set_read(db):
    node = _node(db)
    with raw_plan_seed(db):
        db.execute("UPDATE plan_nodes SET assignee='user', state='failed' WHERE id=?", (node,))  # only a user-owned failure is attention
    for i in range(300):
        _plan_update(db, f"event-{i}", node, kind="failed" if i % 3 == 0 else "done", at=f"2026-09-29T10:{i % 60:02d}:00", thread_id="owner" if i % 2 else "other")
    db.execute("ANALYZE")
    calls = []

    class Capture:
        def execute(self, sql, args=()):
            calls.append((sql, args))
            return db.execute(sql, args)

    ids = ["owner", "other"] + [f"empty-{i}" for i in range(198)]
    display, attention = thread_updates.latest_by_thread_with_attention(Capture(), ids)
    assert set(display) == set(attention) == {"owner", "other"}
    assert len(calls) == 1
    sql, args = calls[0]
    plan = " ".join(row[-1] for row in db.execute("EXPLAIN QUERY PLAN " + sql, args))
    assert "idx_thread_updates_thread_time" in plan, plan
    # `SCAN up` is the retired-branch ancestor walk of one node, not a table scan of `u`.
    assert "SCAN thread_updates" not in plan and not re.search(r"SCAN u\b", plan), plan
    with pytest.raises(ValueError, match="at most 200"):
        thread_updates.latest_by_thread_with_attention(Capture(), ids + ["excess"])
    assert thread_updates.latest_by_thread_with_attention(Capture(), []) == ({}, {})
    assert len(calls) == 1
