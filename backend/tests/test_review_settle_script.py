"""A settle script's report settles a review step that names one; the reviewer's verdict then settles nothing."""

import json

import pytest

from plans import review_settle_script as script, service
from plans.fresh_reviewer import NOT_FRESH
from plans.review_settle import VERDICT_ELSEWHERE
from tests.child_parent_notice_helpers import db
from tests.review_settle_helpers import TWO, act, build_done, ended, hand, harvest, revision_of, run, submit, wakes

__all__ = ["db"]


def scripted(db):
    """A run whose review step names a settle script (``settle:``), its build accepted and the review started."""
    plan, phase, steps = run(db, [TWO[0], {**TWO[1], "settle": "check-totals"}])
    build_done(db, plan.id, steps["build"])
    review = steps["review"]
    assert script.step_settle_script(db, review) == "check-totals"
    service.transition_node(review, "active", db=db)
    return phase, review


def pending(db, review, attempt="a1", version="v1"):
    script.record_pending_settle(db, node_id=review, attempt_id=attempt, reviewer_thread_id="checker", version=version)
    script.bind_settle_run(db, node_id=review, attempt_id=attempt, run_id="run-1")


def report(db, review, *, run_id="run-1", verdict="pass", version="v1", **extra):
    return script.settle_step_from_script(
        db, node_id=review, attempt_id="a1", run_id=run_id, verdict=verdict,
        findings="Totals off for refunds.", version=version, **extra,
    )


def test_a_pass_report_accepts_the_step_and_a_repeat_writes_nothing(db):
    phase, review = scripted(db)
    pending(db, review)

    assert report(db, review) == "accepted"
    count = db.execute("SELECT COUNT(*) FROM plan_node_events").fetchone()[0]
    assert report(db, review) == "accepted"
    assert report(db, review, run_id="run-2", verdict="fail") == "already"

    assert db.execute("SELECT COUNT(*) FROM plan_node_events").fetchone()[0] == count
    assert service._node_row(db, review)["state"] == "accepted"
    assert ended(db, phase)[0]["outcome"] == "completed"


def test_a_fail_report_sends_the_step_back(db):
    _phase, review = scripted(db)
    pending(db, review)

    assert report(db, review, verdict="fail") == "sent_back"

    assert service._node_row(db, review)["state"] == "closed"
    (revision,) = revision_of(db, review)
    assert revision["workflow_step"] == "review"
    assert wakes(db, "review_failed")


def test_a_sent_back_steps_revision_still_settles_from_its_script(db):
    phase, review = scripted(db)
    pending(db, review)
    assert report(db, review, verdict="fail") == "sent_back"
    (revision,) = revision_of(db, review)
    revision_id = revision["id"]

    assert script.step_settle_script(db, revision_id) == "check-totals"
    hand(db, revision_id, "reviewer")
    refused, _leaf = submit(db, "reviewer", verdict="pass")
    assert refused == VERDICT_ELSEWHERE

    script.record_pending_settle(
        db, node_id=revision_id, attempt_id="a2", reviewer_thread_id="checker", version="v2",
    )
    script.bind_settle_run(db, node_id=revision_id, attempt_id="a2", run_id="run-2")
    assert script.settle_step_from_script(
        db, node_id=revision_id, attempt_id="a2", run_id="run-2", verdict="pass",
        findings="Totals right now.", version="v2",
    ) == "accepted"

    assert service._node_row(db, revision_id)["state"] == "accepted"
    assert ended(db, phase)[0]["outcome"] == "completed"


def test_work_that_moved_since_the_review_read_it_goes_back_stale(db):
    _phase, review = scripted(db)
    pending(db, review, version="v1")

    assert report(db, review, version="v2") == "stale"

    assert service._node_row(db, review)["state"] == "closed"
    assert len(revision_of(db, review)) == 1
    assert report(db, review, run_id="run-2") == "already"


@pytest.mark.parametrize("package_wide", [False, True])
def test_no_result_records_its_cause_and_leaves_the_step_in_review(db, package_wide):
    _phase, review = scripted(db)
    pending(db, review)

    assert report(db, review, verdict=None, cause="stopped", package_wide=package_wide) == "no_result"

    assert service._node_row(db, review)["state"] == "active"
    (event,) = db.execute(
        "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event=?", (review, script.NO_RESULT),
    ).fetchall()
    payload = json.loads(event["payload_json"])
    assert (payload["cause"], payload["package_wide"]) == ("stopped", package_wide)
    assert script._pending(db, review, "a1") is None


def test_a_settle_reviewer_that_has_seen_the_work_is_refused(db):
    _phase, review = scripted(db)

    with pytest.raises(service.PlanError) as refused:
        script.record_pending_settle(db, node_id=review, attempt_id="a1", reviewer_thread_id="builder", version="v1")

    assert str(refused.value) == NOT_FRESH.format(worker="builder")
    assert script._pending(db, review, "a1") is None


def test_a_step_with_a_settle_script_is_not_settled_by_its_reviewers_verdict(db):
    _phase, review = scripted(db)
    hand(db, review, "reviewer")
    refused, leaf = submit(db, "reviewer", verdict="pass")
    assert refused == VERDICT_ELSEWHERE
    reviewer_plan = service._node_row(db, leaf)["plan_id"]
    act(db, reviewer_plan, "request_review", leaf, as_thread="reviewer", evidence="Looked; script decides.")

    assert review in harvest(db)

    assert service._node_row(db, review)["state"] == "review_requested"
    assert not wakes(db, "review_passed")
