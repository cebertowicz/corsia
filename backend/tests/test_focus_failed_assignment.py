"""Current persisted human assignment is conjunctive with existing failed suppression."""
import pytest

from tests.test_plan_chat_list_state import (
    db, seed_threads, _thread, _plan, _failed_task, _states, _record_view,
)  # noqa: F401 - fixtures
from tests.plan_close_next_helpers import HUMAN, task
from tests.plan_writer_authorizer import raw_plan_seed


@pytest.mark.parametrize("assignee", ["user", "coordinator", "subagent", None])
def test_only_currently_user_assigned_unseen_failures_are_sidebar_attention(db, assignee):
    actor = _thread(db, "assignment-filter")
    plan = _plan(db, actor.thread_id)
    node = _failed_task(db, plan.id, "Original failure", actor)
    db.execute("UPDATE plan_nodes SET assignee=? WHERE id=?", (assignee, node.id))
    db.commit()
    state = _states(db, [actor.thread_id])[actor.thread_id]
    assert [item["title"] for item in state["attention_items"]] == (["Original failure"] if assignee == "user" else [])
    assert (state["state"] == "failed") is (assignee == "user")
    assert db.execute("SELECT state FROM plan_nodes WHERE id=?", (node.id,)).fetchone()[0] == "failed"


@pytest.mark.parametrize("seen", [False, True])
def test_current_stored_assignment_changes_preserve_acknowledgement(db, seen):
    # A read robustness fixture, not a supported reassignment mutation or websocket journey.
    actor = _thread(db, "assignment-read")
    plan = _plan(db, actor.thread_id)
    node = _failed_task(db, plan.id, "Read-time failure", actor)
    db.execute("UPDATE plan_nodes SET assignee='subagent' WHERE id=?", (node.id,))
    db.commit()
    if seen:
        _record_view(db, HUMAN.user_id, actor.thread_id)
    assert _states(db, [actor.thread_id])[actor.thread_id]["attention_items"] == []
    db.execute("UPDATE plan_nodes SET assignee='user' WHERE id=?", (node.id,))
    db.commit()
    state = _states(db, [actor.thread_id])[actor.thread_id]
    assert (state["state"] == "failed") is (not seen)
    assert len(state["attention_items"]) == (0 if seen else 1)
    db.execute("UPDATE plan_nodes SET assignee='coordinator' WHERE id=?", (node.id,))
    db.commit()
    assert _states(db, [actor.thread_id])[actor.thread_id]["attention_items"] == []


@pytest.mark.parametrize("suppression", ["seen", "archived", "closed", "retired-parent", "follow-up"])
def test_human_assignment_never_resurrects_suppressed_failures(db, suppression):
    actor = _thread(db, "suppressed-failure")
    plan = _plan(db, actor.thread_id)
    node = _failed_task(db, plan.id, "Suppressed failure", actor)
    if suppression == "seen":
        _record_view(db, HUMAN.user_id, actor.thread_id)
    elif suppression == "archived":
        with raw_plan_seed(db):
            db.execute("UPDATE plan_nodes SET archived_at='t' WHERE id=?", (node.id,))
    elif suppression == "closed":
        with raw_plan_seed(db):
            db.execute("UPDATE plan_nodes SET state='closed' WHERE id=?", (node.id,))
    elif suppression == "retired-parent":
        parent = task(db, plan.id, "Retired phase")
        with raw_plan_seed(db):
            db.execute("UPDATE plan_nodes SET state='closed' WHERE id=?", (parent.id,))
            db.execute("UPDATE plan_nodes SET parent_id=? WHERE id=?", (parent.id, node.id))
    else:
        follow_up = task(db, plan.id, "Retry")
        db.execute("UPDATE plan_nodes SET follow_up_of=? WHERE id=?", (node.id, follow_up.id))
    db.commit()
    assert _states(db, [actor.thread_id])[actor.thread_id]["attention_items"] == []
