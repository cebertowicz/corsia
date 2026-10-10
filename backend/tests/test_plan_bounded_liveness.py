"""Plan liveness nudges only work that can move, once per actionable state.

- A watch on one task quiets that task only; the thread's other actionable work
  still arms its wake. There is no thread-wide watch shortcut.
- A stalled wake stays stalled while its actionable state is unchanged, re-arms
  when that state changes, and ends when no actionable work remains.
- Review awaiting a wait is not actionable.
- The watchdog measures from selection, not from when the task became active.
- A title edit is not progress, but rewritten instructions are: the signal
  tracks the work asked for, not how many times its row was written.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from automation import plan_liveness
from automation.continuation.sources.watchdog import WatchdogSource
from infra.schema import SCHEMA_SQL
from plans import admission, mutations, projections, service
from tests.plan_review_helpers import submit_review_evidence


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO threads (id,user_id,created_at,updated_at) VALUES ('owner','u','t','t')"
    )
    db.commit()
    return db


def _task(db, plan_id: str, title: str, **kwargs):
    return service.add_node(
        plan_id, kind="task", title=title,
        acceptance_criteria=f"{title} tests pass", db=db, **kwargs,
    )


def _running(db, plan_id: str, title: str, **kwargs):
    node = _task(db, plan_id, title, **kwargs)
    service.transition_node(node.id, "active", db=db)
    admission.select(node.id, actor_id="owner", provenance="test", db=db, touch=False)
    db.commit()
    return node


def _wake_row(db) -> dict:
    return dict(db.execute("SELECT * FROM plan_liveness_wakes WHERE thread_id='owner'").fetchone())


def test_a_watch_on_one_task_does_not_quiet_the_threads_other_work():
    db = _db()
    plan = service.ensure_plan("thread", "owner", db=db)
    watched = _task(db, plan.id, "Await CI")
    other = _task(db, plan.id, "Write docs")
    for node in (watched, other):
        service.transition_node(node.id, "active", db=db)
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """INSERT INTO expected_event_watches
           (id,owner_thread_id,plan_id,plan_node_id,condition_type,status,armed_at,created_at,updated_at)
           VALUES ('watch-1','owner',?,?,'manual','armed',?,?,?)""",
        (plan.id, watched.id, now, now, now),
    )
    admission.reconcile_plan(plan.id, actor_id="owner", provenance="test", db=db)
    db.commit()

    wake = plan_liveness.on_turn_complete("owner", db=db)

    assert wake is not None
    assert projections.liveness_signal("owner", db=db).node_ids == (other.id,)


def test_a_stalled_wake_stays_quiet_until_the_actionable_state_changes():
    db = _db()
    plan = service.ensure_plan("thread", "owner", db=db)
    first = _running(db, plan.id, "First")
    assert plan_liveness.on_turn_complete("owner", db=db) is not None
    db.execute("UPDATE plan_liveness_wakes SET status='stalled', cycle=2 WHERE thread_id='owner'")
    db.commit()
    stalled = _wake_row(db)

    assert plan_liveness.on_turn_complete("owner", db=db) is None
    assert _wake_row(db) == stalled

    submit_review_evidence(first.id, db=db)
    db.commit()
    rearmed = plan_liveness.on_turn_complete("owner", db=db)

    assert rearmed is not None
    assert (rearmed.status, rearmed.cycle) == ("armed", 0)

    db.execute("UPDATE plan_liveness_wakes SET status='stalled' WHERE thread_id='owner'")
    service.transition_node(
        first.id, "accepted", verification_result="passed", verified_by="reviewer", db=db,
    )
    db.commit()

    assert plan_liveness.on_turn_complete("owner", db=db) is None
    assert _wake_row(db)["status"] == "cancelled"


def test_review_awaiting_a_wait_is_not_actionable():
    db = _db()
    plan = service.ensure_plan("thread", "owner", db=db)
    prerequisite = _task(db, plan.id, "Prerequisite")
    reviewed = _task(db, plan.id, "Reviewed")
    service.add_dependency(reviewed.id, prerequisite.id, db=db)
    service.transition_node(reviewed.id, "active", db=db)
    service.transition_node(reviewed.id, "review_requested", db=db)
    db.commit()

    assert reviewed.id not in projections.liveness_signal("owner", db=db).node_ids


def test_the_watchdog_measures_from_selection_not_activation():
    db = _db()
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _task(db, plan.id, "Long running", watchdog_budget_seconds=60)
    service.transition_node(node.id, "active", db=db)
    now = datetime.now(timezone.utc)
    # Active since now, but selected only 90 seconds later: the budget runs from selection.
    with patch("plans.service._now", return_value=(now + timedelta(seconds=90)).isoformat()):
        admission.select(node.id, actor_id="owner", provenance="test", db=db, touch=False)
    db.commit()
    source = WatchdogSource()

    assert source.due((now + timedelta(seconds=120)).isoformat(), db=db) == []
    assert len(source.due((now + timedelta(seconds=200)).isoformat(), db=db)) == 1


def test_a_title_edit_is_not_progress_but_new_instructions_are():
    db = _db()
    plan = service.ensure_plan("thread", "owner", db=db)
    node = _running(db, plan.id, "Draft")
    before = projections.liveness_signal("owner", db=db)

    current = service.get_node(node.id, db=db)
    mutations.edit_node(node.id, title="Draft the brief", expected_revision=current.revision, db=db)
    db.commit()
    renamed = projections.liveness_signal("owner", db=db)

    assert renamed.signal_hash == before.signal_hash

    current = service.get_node(node.id, db=db)
    mutations.edit_node(
        node.id, description="Cover the rollout risks too.",
        expected_revision=current.revision, db=db,
    )
    db.commit()

    assert projections.liveness_signal("owner", db=db).signal_hash != renamed.signal_hash
