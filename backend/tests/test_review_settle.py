"""A reviewer's verdict settles its review step: a pass accepts it, a fail sends it back to the author."""

import json

from plans import service
from plans.review_settle import REVIEW_FAILED, VERDICT_ELSEWHERE, VERDICT_REQUIRED
from prompts import plan_wakes as wakes_text
from tests.child_parent_notice_helpers import db
from tests.plan_review_helpers import submit_review_evidence
from tests.review_settle_helpers import (
    ACCEPT, AUTHOR, FINDINGS, THREE, act, build_done, ended, hand, harvest, revision_of, run, submit, wakes, work,
)

__all__ = ["db"]


def test_a_pass_accepts_the_review_and_ends_the_run(db):
    plan, phase, steps = run(db)
    build_done(db, plan.id, steps["build"])
    hand(db, steps["review"], "reviewer")
    _result, leaf = submit(db, "reviewer", verdict="pass", evidence="Checked totals and heading; both right.")

    assert steps["review"] in harvest(db)

    assert service._node_row(db, steps["review"])["state"] == "accepted"
    assert service._node_row(db, leaf)["state"] == "accepted"
    (record,) = ended(db, phase)
    assert record["outcome"] == "completed"
    (told,) = wakes(db, "review_passed")
    assert told.startswith(wakes_text.WAKE_REVIEW_PASSED.format(title="review"))
    assert json.loads(told.split("\n", 1)[1])["review"]["evidence"] == "Checked totals and heading; both right."
    assert not [w for w in wakes(db, "child_returned") if " for review " in w.split("\n", 1)[0]]
    assert not wakes(db, "task_closed")


def test_a_fail_sends_the_review_back_to_its_author_and_keeps_the_run_open(db):
    plan, phase, steps = run(db, THREE)
    build_done(db, plan.id, steps["build"])
    hand(db, steps["review"], "reviewer")
    _result, leaf = submit(db, "reviewer", verdict="fail")

    assert steps["review"] in harvest(db)

    review = service._node_row(db, steps["review"])
    assert (review["state"], review["sent_back"]) == ("closed", 1)
    reason = db.execute(
        "SELECT payload_json FROM plan_node_events WHERE node_id=? AND new_state='closed'", (review["id"],),
    ).fetchone()
    assert json.loads(reason["payload_json"])["reason"].startswith(REVIEW_FAILED)
    rejected = db.execute(
        "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event='review_rejected'", (review["id"],),
    ).fetchone()
    assert json.loads(rejected["payload_json"])["feedback"] == FINDINGS
    assert service._node_row(db, leaf)["state"] == "accepted"
    (revision,) = revision_of(db, review["id"])
    assert (revision["workflow_step"], revision["state"], revision["spawned_thread_id"]) == ("review", "pending", None)
    assert db.execute(
        "SELECT depends_on_node_id FROM plan_node_dependencies WHERE node_id=?", (steps["ship"],),
    ).fetchall()[0]["depends_on_node_id"] == revision["id"]
    # No seeded worker step, no stand-down: the reviewer is idle and reusable.
    reviewer_plan = service.ensure_plan("thread", "reviewer", db=db)
    assert not db.execute(
        "SELECT 1 FROM plan_nodes WHERE plan_id=? AND state IN ('pending','active')", (reviewer_plan.id,),
    ).fetchall()
    assert db.execute("SELECT archived_at FROM threads WHERE id='reviewer'").fetchone()["archived_at"] is None
    assert not db.execute("SELECT 1 FROM plan_nodes WHERE state='failed'").fetchall()
    assert ended(db, phase) == []
    (told,) = wakes(db, "review_failed")
    assert told.startswith(wakes_text.WAKE_REVIEW_FAILED.format(
        title="review", revision_id=revision["id"], reviewer="reviewer",
    ))
    assert not wakes(db, "revision_requested", "reviewer") and not wakes(db, "revision_requested")


def test_a_failed_ordinary_required_step_still_ends_the_run(db):
    plan, phase, steps = run(db)
    service.transition_node(steps["build"], "active", db=db)

    act(db, plan.id, "fail", steps["build"], evidence="Could not build")

    (record,) = ended(db, phase)
    assert record["outcome"] == "failed"


def test_a_reviewer_must_give_a_verdict(db):
    _plan, _phase, steps = run(db)
    hand(db, steps["review"], "reviewer")

    refused, leaf = submit(db, "reviewer")

    assert refused == VERDICT_REQUIRED
    assert service._node_row(db, leaf)["state"] == "active"


def test_a_verdict_anywhere_else_is_refused(db):
    plan, _phase, steps = run(db)
    hand(db, steps["build"], "builder")
    assert submit(db, "builder", verdict="pass")[0] == VERDICT_ELSEWHERE
    own = service.add_node(plan.id, kind="task", title="Own", acceptance_criteria="Done", db=db)
    service.transition_node(own.id, "active", db=db)
    assert act(db, plan.id, "request_review", own.id, evidence="Done", verdict="fail") == VERDICT_ELSEWHERE


def test_a_return_without_a_verdict_is_judged_by_the_author_as_before(db):
    plan, _phase, steps = run(db)
    build_done(db, plan.id, steps["build"])
    hand(db, steps["review"], "reviewer")
    _plan_id, leaf = work(db, "reviewer")
    submit_review_evidence(leaf, db=db, actor_id="reviewer")  # a reviewer started before verdicts

    assert steps["review"] in harvest(db)

    assert service._node_row(db, steps["review"])["state"] == "review_requested"
    assert [w for w in wakes(db, "child_returned") if " for review " in w.split("\n", 1)[0]]
    assert not wakes(db, "review_passed") and not wakes(db, "review_failed")


def test_a_reviewer_that_built_the_work_settles_nothing(db):
    plan, _phase, steps = run(db)
    build_done(db, plan.id, steps["build"])
    hand(db, steps["review"], "builder")
    submit(db, "builder", verdict="pass")

    assert steps["review"] in harvest(db)

    assert service._node_row(db, steps["review"])["state"] == "review_requested"
    assert not wakes(db, "review_passed")


def test_a_harvest_whose_author_wake_cannot_land_rolls_the_settle_back(db):
    plan, _phase, steps = run(db, THREE)
    build_done(db, plan.id, steps["build"])
    hand(db, steps["review"], "reviewer")
    _result, leaf = submit(db, "reviewer", verdict="fail")
    db.execute("UPDATE threads SET model='' WHERE id=?", (AUTHOR,))

    assert harvest(db) == []

    assert service._node_row(db, steps["review"])["state"] == "active"
    assert service._node_row(db, leaf)["state"] == "review_requested"
    assert revision_of(db, steps["review"]) == []
    assert not db.execute(
        "SELECT 1 FROM plan_node_events WHERE node_id=? AND event IN ('review_rejected','child_plan_completed')",
        (steps["review"],),
    ).fetchall()

    db.execute("UPDATE threads SET model='native/model' WHERE id=?", (AUTHOR,))
    assert steps["review"] in harvest(db)
    assert service._node_row(db, steps["review"])["state"] == "closed"


def test_the_person_may_still_settle_a_review_step(db):
    plan, _phase, steps = run(db)
    service.transition_node(steps["review"], "active", db=db)
    assert act(db, plan.id, "mark_done", steps["review"], user=True, **ACCEPT)["node"]["state"] == "accepted"
