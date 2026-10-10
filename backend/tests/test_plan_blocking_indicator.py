"""The plan's whole relationship to a decision: a compact waiting-on-you indicator.

A plan can be *blocked on* a decision, says so, and unblocks when it is answered.
It does not render the decision. So the operator envelope publishes a count and
no decision bodies — and that count must include decisions raised on attached
child plans, or a blocked parent reads zero while the user's dock shows the
child's question.

Red at `19ea844a`: the envelope publishes decision bodies, and its count is
plan-scoped in a way that drops every attached child plan.
"""

from __future__ import annotations

from collections.abc import Iterator

import sqlite3

import pytest

from infra.schema import SCHEMA_SQL
import plans.decisions as decisions

import plans.operator_projection as operator_projection
import plans.service as service
from plans.boundary import PlanActor
from tests.test_attention_queue import _mid, _seed, raise_decision  # noqa: F401


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    _seed(connection)
    connection.commit()
    yield connection
    connection.close()


def _human() -> PlanActor:
    return PlanActor(None, "user-1", None, kind="user")


def _attach_child(db, parent_thread: str, child_thread: str) -> None:
    """Make `child_thread`'s plan a real attached child of `parent_thread`'s."""
    parent = service.ensure_plan("thread", parent_thread, db=db)
    node = service.add_node(
        parent.id, kind="task", title=f"Delegate to {child_thread}",
        acceptance_criteria="Child returns its result", db=db,
    )
    db.commit()
    service.transition_node(
        node.id, "active", actor_id=parent_thread, provenance="p", db=db,
    )
    db.execute(
        "UPDATE plan_nodes SET spawned_thread_id=? WHERE id=?", (child_thread, node.id),
    )
    child = service.ensure_plan("thread", child_thread, db=db)
    db.execute(
        "UPDATE plans SET parent_plan_id=? WHERE id=?", (parent.id, child.id),
    )
    db.commit()


def _envelope_thread(db, thread_id: str) -> dict:
    return operator_projection.read_thread(thread_id, actor=_human(), db=db)



# --- the count must reach attached children ----------------------------------


def test_a_parent_plan_counts_a_decision_raised_on_its_attached_child(db):
    _attach_child(db, "root", "child")
    raise_decision(db, "child", "child")

    assert _envelope_thread(db, "root")["attention"]["awaiting_count"] == 1


def test_a_parent_plan_counts_a_decision_two_levels_down(db):
    _attach_child(db, "root", "child")
    _attach_child(db, "child", "grandchild")
    raise_decision(db, "grandchild", "grandchild")

    assert _envelope_thread(db, "root")["attention"]["awaiting_count"] == 1


def test_the_indicator_count_and_the_user_queue_cannot_disagree(db):
    """The residual the exact-head review named: zero here, one in the dock."""
    from tests.test_attention_queue import queue_for

    _attach_child(db, "root", "child")
    raise_decision(db, "child", "child")

    assert len(queue_for(db)) == 1
    assert _envelope_thread(db, "root")["attention"]["awaiting_count"] == 1


def test_an_answered_decision_stops_being_counted(db):
    _attach_child(db, "root", "child")
    decision = raise_decision(db, "child", "child")
    decisions.answer(
        decision["id"], option_id="A", expected_revision=decision["revision"],
        mutation_id=_mid(), actor=_human(), provenance="p", db=db,
    )
    db.commit()

    assert _envelope_thread(db, "root")["attention"]["awaiting_count"] == 0


def test_an_unrelated_thread_is_not_counted_as_blocked(db):
    _attach_child(db, "root", "child")
    raise_decision(db, "other-root", "other-root")

    assert _envelope_thread(db, "root")["attention"]["awaiting_count"] == 0


def test_a_decision_still_in_draft_does_not_block_the_plan(db):
    _attach_child(db, "root", "child")
    plan = service.ensure_plan("thread", "child", db=db)
    node = service.add_node(
        plan.id, kind="task", title="Child work", acceptance_criteria="DOD", db=db,
    )
    db.commit()
    decisions.draft(
        plan.id, source_node_id=node.id, mutation_id=_mid(),
        actor=PlanActor("child", "user-1", "project-1"), provenance="p", db=db,
        title="Not raised yet", context="c", tradeoff="t", recommendation="A",
        rationale="r", other_enabled=True,
        options=[{"id": "A", "label": "One", "detail": ""},
                 {"id": "B", "label": "Two", "detail": ""}],
    )
    db.commit()

    assert _envelope_thread(db, "root")["attention"]["awaiting_count"] == 0


# --- the plan contract no longer carries decision bodies ---------------------


def test_the_operator_envelope_publishes_no_decision_body(db):
    """The full decision is not plan UI — the plan states only that it blocks."""
    _attach_child(db, "root", "child")
    raise_decision(db, "child", "child")

    envelope = _envelope_thread(db, "root")

    assert "decisions" not in envelope["attention"]
    assert "Where should decision state live?" not in str(envelope)
    assert "Reuse the identity" not in str(envelope)


def test_the_revision_token_still_moves_when_the_awaiting_count_changes(db):
    _attach_child(db, "root", "child")
    before = _envelope_thread(db, "root")["revision_token"]

    raise_decision(db, "child", "child")

    assert _envelope_thread(db, "root")["revision_token"] != before
