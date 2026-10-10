"""The chat-list row carries each thread's newest update record (update cards r2).

The update rides beside the row's state word: it never changes the word, the
owed or failed precedence, or the attention list, and a planless thread with
an explicit send still gets a row.
"""

from __future__ import annotations

import pytest

from infra import thread_updates
from plans import operator_chat_state
from tests.plan_close_next_helpers import HUMAN, plan_update, revision, task
from tests.test_plan_chat_list_state import _awaiting_access_request, _plan, _thread
from tests.test_plan_reconcile_release import db, seed_threads  # noqa: F401 - fixture

_AT = "2026-09-29T10:00:00"


def _states(db, ids: list[str]) -> dict:
    return operator_chat_state.read_thread_states(ids, actor=HUMAN, db=db)["states"]


def _sent(db, thread_id: str, message_id: str, at: str = _AT) -> None:
    thread_updates.record_marked(
        db, thread_id=thread_id, message_id=message_id, node_id=None, created_at=at,
    )
    db.commit()


def test_a_planless_explicit_send_reaches_its_row_as_idle_with_the_update(db):
    _thread(db, "planless")
    _sent(db, "planless", "m-1")

    assert _states(db, ["planless"])["planless"] == {
        "state": "idle",
        "label_key": None,
        "paused": False,
        "attention_items": [],
        "stuck_overdue_waits": 0,
        "next_overdue_at": None,
        "settled": False,
        "latest_update": {
            "id": 1,
            "thread_id": "planless",
            "kind": "marked",
            "message_id": "m-1",
            "node_id": None,
            "title": None,
            "after_seq": 0,
            "created_at": "2026-09-29T10:00:00.000000",
            "summary": None,
        },
        "has_update": True,
        "opened": False,
    }


def test_a_row_without_updates_reads_none(db):
    _thread(db, "quiet")
    task(db, _plan(db, "quiet").id, "Later")

    assert _states(db, ["quiet"])["quiet"]["latest_update"] is None


def test_the_row_carries_only_the_newest_update(db):
    _thread(db, "busy")
    _sent(db, "busy", "m-old", "2026-09-29T09:00:00")
    _sent(db, "busy", "m-new", "2026-09-29T11:00:00")

    assert _states(db, ["busy"])["busy"]["latest_update"]["message_id"] == "m-new"


def test_an_update_never_outranks_what_the_user_owes_or_a_failure(db):
    failed_actor = _thread(db, "failed")
    failed_plan = _plan(db, "failed")
    failed = task(db, failed_plan.id, "Ship the report", assignee="user")
    plan_update(db, failed_plan.id, "activate", {
        "node_id": failed.id, "expected_revision": revision(db, failed.id),
    }, actor=failed_actor)
    plan_update(db, failed_plan.id, "fail", {
        "node_id": failed.id, "expected_revision": revision(db, failed.id),
    }, actor=failed_actor)
    _sent(db, "failed", "m-failed")

    _thread(db, "owed")
    _awaiting_access_request(db, "owed")
    _sent(db, "owed", "m-owed")

    states = _states(db, ["failed", "owed"])
    assert states["failed"]["state"] == "failed"
    assert [item["kind"] for item in states["failed"]["attention_items"]] == ["failed"]
    assert states["failed"]["latest_update"]["kind"] == "failed"
    assert states["failed"]["latest_update"]["message_id"] is None
    assert states["failed"]["latest_update"]["node_id"] == failed.id
    assert states["owed"]["state"] == "blocked_on_you"
    assert [item["kind"] for item in states["owed"]["attention_items"]] == ["access"]
    assert states["owed"]["latest_update"]["message_id"] == "m-owed"


def test_another_users_or_archived_threads_updates_are_never_read(db):
    _thread(db, "foreign", user_id="someone-else")
    _sent(db, "foreign", "m-foreign")
    _thread(db, "archived")
    db.execute("UPDATE threads SET archived_at='2026-09-01T00:00:00', lifecycle_state='archived' WHERE id='archived'")
    _sent(db, "archived", "m-archived")

    assert _states(db, ["foreign", "archived"]) == {}


def test_the_row_read_stays_capped(db):
    with pytest.raises(ValueError, match="at most 200"):
        _states(db, [f"t{index}" for index in range(201)])


def _visited(db, thread_id: str, entered: str, exited: str | None) -> None:
    db.execute(
        "INSERT INTO thread_presence (user_id,thread_id,present,last_entered_at,"
        "last_exited_at,last_confirmed_at,updated_at) VALUES (?,?,?,?,?,?,?)",
        (HUMAN.user_id, thread_id, 0 if exited else 1, entered, exited, entered, entered),
    )
    db.commit()


def test_an_opened_planless_thread_gets_a_row_it_can_mark_unread(db):
    _thread(db, "opened")
    _visited(db, "opened", "2026-09-29T09:00:00.000000", "2026-09-29T09:30:00.000000")

    row = _states(db, ["opened"])["opened"]

    assert (row["state"], row["has_update"], row["opened"], row["latest_update"]) == (
        "idle", False, True, None,
    )


def test_the_dot_is_reported_beside_a_higher_mark_for_the_client_to_rank(db):
    failed_actor = _thread(db, "failed-seen")
    failed_plan = _plan(db, "failed-seen")
    failed = task(db, failed_plan.id, "Ship the report", assignee="user")
    for operation in ("activate", "fail"):
        plan_update(db, failed_plan.id, operation, {
            "node_id": failed.id, "expected_revision": revision(db, failed.id),
        }, actor=failed_actor)
    _visited(db, "failed-seen", "2026-09-29T08:00:00.000000", "2026-09-29T09:00:00.000000")
    _sent(db, "failed-seen", "m-after-exit")

    row = _states(db, ["failed-seen"])["failed-seen"]

    assert (row["state"], row["has_update"], row["opened"]) == ("failed", True, True)
    assert [item["kind"] for item in row["attention_items"]] == ["failed"]


def _accepted(db, plan_id: str, title: str, actor) -> str:
    node = task(db, plan_id, title)
    plan_update(db, plan_id, "activate", {
        "node_id": node.id, "expected_revision": revision(db, node.id),
    }, actor=actor)
    plan_update(db, plan_id, "accept", {
        "node_id": node.id, "evidence": "Shipped", "verification_result": "passed",
        "expected_revision": revision(db, node.id),
    }, actor=actor)
    return node.id


def test_done_is_a_plan_with_finished_work_nothing_open_and_no_pause(db):
    done_actor = _thread(db, "done")
    _accepted(db, _plan(db, "done").id, "Ship it", done_actor)

    open_actor = _thread(db, "still-open")
    open_plan = _plan(db, "still-open")
    _accepted(db, open_plan.id, "First", open_actor)
    task(db, open_plan.id, "Second")

    paused_actor = _thread(db, "paused")
    paused_plan = _plan(db, "paused")
    _accepted(db, paused_plan.id, "Ship it", paused_actor)
    db.execute("UPDATE plans SET state='paused' WHERE id=?", (paused_plan.id,))

    retired_actor = _thread(db, "retired")
    retired_plan = _plan(db, "retired")
    retired = _accepted(db, retired_plan.id, "Archived", retired_actor)
    db.execute("UPDATE plan_nodes SET archived_at='t' WHERE id=?", (retired,))

    _thread(db, "empty")
    _plan(db, "empty")
    _thread(db, "planless-done")
    _sent(db, "planless-done", "m-9")
    db.commit()

    states = _states(db, ["done", "still-open", "paused", "retired", "empty", "planless-done"])

    assert {thread_id: row["settled"] for thread_id, row in states.items()} == {
        "done": True, "still-open": False, "paused": False, "retired": False,
        "empty": False, "planless-done": False,
    }
