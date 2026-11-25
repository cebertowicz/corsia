"""The priorities line shows only on a Later task's own events, in its shipped words."""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from infra.schema import SCHEMA_SQL
from plans import later_pull, later_review, scheduled_wait, service
from plans.boundary import PlanActor, mutate
from prompts import later_review as later_review_text
from tests.plan_scale_fixture import build
from tests.test_plan_open_tree_scale import SAMPLE_EVERY, SIZES, STEP_TOLERANCE, measure
from prompts.later_pull import DUE_IN_LATER, LEFT_LATER
import tools.plans  # noqa: F401  (the tool layer that calls these helpers imports cleanly)

OWNER = PlanActor("owner", "user", "project")


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project','Project','t','t')"
    )
    connection.execute(
        """INSERT INTO threads (id,user_id,project_id,model,created_at,updated_at)
           VALUES ('owner','user','project','test/model','t','t')"""
    )
    connection.commit()
    yield connection
    connection.close()


def _call(db, plan_id, operation, **params):
    out = mutate(plan_id, operation, params, actor=OWNER, provenance="test.later_pull",
                 mutation_id=uuid.uuid4().hex, db=db).result
    db.commit()
    return out


def _rev(db, node_id):
    return service._node_row(db, node_id)["revision"]


def _task(db, *, later: bool, watch: bool = True):
    plan = service.ensure_plan("thread", "owner", db=db)
    node = service.add_node(plan.id, kind="task", title="Reprint the map",
                            acceptance_criteria="Printed", db=db)
    db.commit()
    if not watch:
        _call(db, plan.id, "edit", node_id=node.id, later=True, reason="Shop closed",
              expected_revision=_rev(db, node.id))
        return plan, node, None
    _call(db, plan.id, "arm_watch", node_id=node.id, reason="Shop closed",
          condition={"type": "deadline", "value": "2099-01-01T00:00:00+00:00"},
          expected_revision=_rev(db, node.id))
    if later:
        _call(db, plan.id, "edit", node_id=node.id, later=True, reason="Shop closed",
              expected_revision=_rev(db, node.id))
    armed = db.execute(
        "SELECT id, plan_node_id, condition_type, condition_value FROM expected_event_watches"
        " WHERE plan_node_id=? AND status='armed'", (node.id,),
    ).fetchone()
    return plan, node, type("Watch", (), {
        "id": armed["id"], "plan_node_id": node.id,
        "condition": {"type": "deadline", "value": armed["condition_value"]},
    })


def test_due_wake_on_a_later_task_carries_the_check(db):
    _plan, _node, watch = _task(db, later=True)
    summary = scheduled_wait.wake_summary(db, watch)
    assert summary.endswith(DUE_IN_LATER.format(criteria="Done when: Printed."))
    assert "Start it now" not in summary


def test_a_done_when_that_ends_a_sentence_gets_no_second_full_stop(db):
    plan, node, watch = _task(db, later=True)
    _call(db, plan.id, "edit", node_id=node.id, acceptance_criteria="Printed and posted.",
          expected_revision=_rev(db, node.id))
    summary = scheduled_wait.wake_summary(db, watch)
    assert "Done when: Printed and posted. Judge fit on that" in summary


# The three messages exactly as approved. A change here is a change to shipped
# wording, which goes back for approval before it merges.
FIT = (
    "read the project's priorities and yours now (none written means nothing fits). "
    "If it fits, go ahead. If it doesn't, starting it is the person's call, not a detail "
    "you settle, whatever its size, cost, urgency or how easily it is undone: ask them now, "
    "put any urgency in your recommendation, and don't work it until they answer. "
    "An earlier approval that doesn't fit them doesn't count."
)


def test_the_three_messages_are_the_approved_words():
    assert DUE_IN_LATER.format(criteria="Done when: X.") == (
        "It is in Later. Done when: X. Judge fit on that, not the title: before you start it, "
        f"{FIT} Start it, schedule it again with a reason, close it, leave it in Later, "
        "or raise a decision."
    )
    assert LEFT_LATER.format(title="T") == (
        f'"T" was in Later: before you work it, {FIT} If you ask, put it back with edit later=true.'
    )
    assert later_review_text.LATER_REVIEW_MOVES == (
        "Close every one no longer needed in one close_node call with node_ids. Promote one "
        "(plan_update edit later=false) only if it is due now and what it delivers, not its "
        "title, fits the project's priorities and your own, read now; no priorities written "
        "means nothing fits. If one doesn't fit, promoting it is the person's call, not a "
        "detail you settle, whatever its size, cost, urgency or how easily it is undone: ask "
        "them now, put any urgency in your recommendation, and leave it in Later until they "
        "answer. An earlier approval that doesn't fit them doesn't count. Re-rank with reorder, and "
        "leave the rest in Later."
    )


def test_long_criteria_are_cut_out_loud(db):
    _plan, node, _watch = _task(db, later=True)
    line = later_review.criteria_text("x" * 2000, node.id)
    assert line.endswith(f"[cut; plan_read(node_id='{node.id}') returns the whole task]")
    assert later_review.criteria_text("", node.id).startswith("Done when: none written")


def test_weekly_rows_carry_what_each_task_delivers(db):
    plan, node, _watch = _task(db, later=True)
    text = later_review.wake_text(db, later_review.pile(db, plan.id))
    assert f"(node {node.id}," in text
    assert "\n  Done when: Printed\n" in text


def test_due_wake_on_running_work_is_unchanged(db):
    _plan, _node, watch = _task(db, later=False)
    summary = scheduled_wait.wake_summary(db, watch)
    assert "priorities" not in summary
    assert summary.endswith("Start it now, schedule it again with a reason, close it, "
                            "move it to Later, or raise a decision.")


def test_a_start_that_takes_a_task_out_of_later_says_so_once(db):
    plan, node, _watch = _task(db, later=True, watch=False)
    params = {"node_id": node.id, "expected_revision": _rev(db, node.id)}
    before = later_pull.titles_in_later(db, plan.id, params)
    result = _call(db, plan.id, "start", **params)
    later_pull.tell_left_later(db, before, result)
    assert result["notice"].startswith('"Reprint the map" was in Later: before you work it,')
    again = {}
    later_pull.tell_left_later(db, later_pull.titles_in_later(db, plan.id, params), again)
    assert "notice" not in again


def test_a_bulk_edit_out_of_later_says_so_once_per_task(db):
    plan, first, _watch = _task(db, later=True, watch=False)
    second = service.add_node(plan.id, kind="task", title="Book the hall",
                              acceptance_criteria="Booked", db=db)
    db.commit()
    _call(db, plan.id, "edit", node_id=second.id, later=True, reason="Hall closed",
          expected_revision=_rev(db, second.id))
    params = {"node_ids": [first.id, second.id], "later": False}
    before = later_pull.titles_in_later(db, plan.id, params)
    result = _call(db, plan.id, "edit", **params)
    later_pull.tell_left_later(db, before, result)
    lines = result["notice"].split("\n")
    assert [line.split(" was in Later")[0] for line in lines] == ['"Reprint the map"', '"Book the hall"']


@pytest.mark.parametrize("call", ["named", "weekly"])
def test_the_added_reads_cost_the_same_whatever_the_plan_history(call):
    """A bulk call naming several tasks, and the weekly review's done-when read, stay flat."""
    def steps(size):
        scale = build(size)
        rows = scale.db.execute(
            "SELECT id FROM plan_nodes WHERE plan_id=? AND kind='task' AND state IN ('pending','active')"
            " LIMIT 5", (scale.plan_id,),
        ).fetchall()
        ids = [row["id"] for row in rows]
        scale.db.execute(f"UPDATE plan_nodes SET later=1 WHERE id IN ({','.join('?' * len(ids))})", ids)
        read = (
            (lambda: later_pull.titles_in_later(scale.db, scale.plan_id, {"node_ids": ids}))
            if call == "named" else (lambda: later_review._criteria(scale.db, ids))
        )
        return measure(scale.db, read)[0]

    small, large = steps(SIZES[0]), steps(SIZES[-1])
    assert large <= max(small, SAMPLE_EVERY) * STEP_TOLERANCE, (small, large)


def test_the_plan_tool_adds_the_line_when_its_call_takes_a_task_out_of_later(db, monkeypatch):
    """The real wiring: the read runs before the mutation and the line is added after it."""
    from tests.test_current_plan_bounded_read import _tool_env

    plan, node, _watch = _task(db, later=True, watch=False)
    tools = _tool_env(db, monkeypatch)
    result = tools._mutation_operation(
        "owner", {"user_id": "user", "project_id": "project"}, None, plan.id, "start",
        {"node_id": node.id, "expected_revision": _rev(db, node.id)}, "wiring-start", {},
    )
    assert result.result["notice"].startswith('"Reprint the map" was in Later: before you work it,')


def test_the_line_cuts_a_long_title_like_every_plan_surface(db):
    from plans.close_next_bounds import TITLE_MAX_BYTES, json_bytes

    plan, node, _watch = _task(db, later=True, watch=False)
    db.execute("UPDATE plan_nodes SET title=? WHERE id=?", ('"Map" ' * 200, node.id))
    db.commit()
    title = later_pull.titles_in_later(db, plan.id, {"node_id": node.id})[node.id]
    assert json_bytes(title) <= TITLE_MAX_BYTES
