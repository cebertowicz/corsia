"""The last settled item of a delivered agent batch settles the owner's feedback task."""

from __future__ import annotations

from pathlib import Path

import pytest

from doc_feedback_helpers import add_comment, batches, seed_document, seed_thread
from doceditor import comment_actions, comments, feedback_batches
from infra.database import get_db, write_transaction
from plans import admission, admission_attempts, holds, lifecycle, rejection, service
from plans.feedback_nodes import settle_feedback_node
from tests.plan_review_helpers import submit_review_evidence


def _delivered(db, *, suggestions: int = 1, comments_: int = 0, edit: bool = False):
    """One delivered agent batch: (artifact id, suggestion rows, comment rows, node id, batch id)."""
    seed_thread(db, "owner", title="Documents")
    seed_thread(db, "author", title="Reviewer")
    artifact_id = seed_document(db, "owner", name="Design.md")
    suggested = [add_comment(db, artifact_id, replacement=f"New {n}")[0] for n in range(suggestions)]
    plain = [add_comment(db, artifact_id)[0] for _ in range(comments_)]
    if edit:
        feedback_batches.record_edit(
            db, artifact_id=artifact_id, recipient_id="owner", author_kind="agent", author_id="author",
            actor_display="Reviewer", path=Path("/workspace/shared/Design.md"), before=b"old\n", after=b"new\n",
        )
        db.commit()
    batch = batches(db, state="open")[0]
    feedback_batches.close_batch(db, batch["id"], "quiet")
    delivered = batches(db)[0]
    return artifact_id, suggested, plain, delivered["delivered_node_id"], delivered["id"]


def _decide(db, row: dict, decision: str = "accept") -> None:
    with write_transaction(db, scope="test_feedback_settlement") as connection:
        comment_actions.record_decision(
            row["id"], decision=decision, decided_by="Robin", decided_by_thread_id=None, db=connection,
        )


def _state(db, node_id: str) -> str:
    return service.get_node(node_id, db=db).state


def test_the_last_decided_suggestion_settles_the_task_with_each_outcome_as_evidence(isolated_db):
    db = get_db()
    try:
        _, suggested, _, node_id, _ = _delivered(db, suggestions=2)

        _decide(db, suggested[0], "accept")
        assert _state(db, node_id) != "accepted"
        _decide(db, suggested[1], "reject")

        assert _state(db, node_id) == "accepted"
        text = db.execute(
            "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event='review_evidence_submitted'",
            (node_id,),
        ).fetchone()["payload_json"]
        assert f"Comment {suggested[0]['id']}: suggestion accepted by Robin" in text
        assert f"Comment {suggested[1]['id']}: suggestion rejected by Robin" in text
    finally:
        db.close()


def test_a_resolved_comment_counts_as_settled(isolated_db):
    db = get_db()
    try:
        _, _, plain, node_id, _ = _delivered(db, suggestions=0, comments_=1)

        comments.update(plain[0]["id"], {"resolved": True}, db=db)

        assert _state(db, node_id) == "accepted"
    finally:
        db.close()


def test_a_pending_suggestion_keeps_the_task_open(isolated_db):
    db = get_db()
    try:
        _, suggested, _, node_id, _ = _delivered(db, suggestions=2)

        _decide(db, suggested[0])

        assert _state(db, node_id) != "accepted"
    finally:
        db.close()


def test_an_acknowledged_comment_is_still_owed_judgment(isolated_db):
    db = get_db()
    try:
        _, suggested, plain, node_id, _ = _delivered(db, suggestions=1, comments_=1)

        comments.update(plain[0]["id"], {"acked": True}, db=db)
        _decide(db, suggested[0])

        assert _state(db, node_id) != "accepted"
        comments.update(plain[0]["id"], {"resolved": True}, db=db)
        assert _state(db, node_id) == "accepted"
    finally:
        db.close()


def test_a_batch_that_carried_an_edit_is_never_settled_by_its_comments(isolated_db):
    db = get_db()
    try:
        _, suggested, _, node_id, _ = _delivered(db, suggestions=1, edit=True)

        _decide(db, suggested[0])

        assert _state(db, node_id) != "accepted"
    finally:
        db.close()


def test_a_task_the_owner_already_finished_is_left_alone(isolated_db):
    db = get_db()
    try:
        _, suggested, _, node_id, batch_id = _delivered(db, suggestions=1)
        settle_feedback_node(db, "owner", batch_id, evidence="Done by the owner")
        events = db.execute("SELECT COUNT(*) FROM plan_node_events WHERE node_id=?", (node_id,)).fetchone()[0]

        _decide(db, suggested[0])
        settle_feedback_node(db, "owner", batch_id, evidence="Again")

        assert _state(db, node_id) == "accepted"
        assert db.execute("SELECT COUNT(*) FROM plan_node_events WHERE node_id=?", (node_id,)).fetchone()[0] == events
    finally:
        db.close()


def _description_and_events(db, node_id: str) -> tuple[str, list[str]]:
    node = service.get_node(node_id, db=db)
    events = [row["event"] for row in db.execute("SELECT event FROM plan_node_events WHERE node_id=?", (node_id,))]
    return node.description, events


def _pause_plan(db, node_id: str) -> None:
    plan = service.ensure_plan("thread", "owner", db=db)
    lifecycle.transition(plan.id, "paused", expected_revision=plan.revision, actor_id="owner", provenance="test", db=db)
    db.commit()


def _hold_step(db, node_id: str) -> None:
    holds.pause(node_id, "Waiting", expected_revision=service._node_row(db, node_id)["revision"],
                actor_id="owner", provenance="test", db=db)
    db.commit()


@pytest.mark.parametrize("stop", [_pause_plan, _hold_step], ids=["paused_plan", "held_step"])
def test_a_stopped_task_keeps_its_state_gets_a_note_and_the_resolve_still_commits(isolated_db, stop):
    db = get_db()
    try:
        _, _, plain, node_id, _ = _delivered(db, suggestions=0, comments_=1)
        before = service.get_node(node_id, db=db)
        stop(db, node_id)
        state = _state(db, node_id)

        comments.update(plain[0]["id"], {"resolved": True}, db=db)

        description, events = _description_and_events(db, node_id)
        assert _state(db, node_id) == state != "accepted"
        assert f"Comment {plain[0]['id']}: resolved" in description
        assert "Mark it done once it resumes." in description and before.description in description
        assert f"resolved\n\nThe user settled every item while this task was paused or held" in description
        assert events.count("feedback_settled_while_stopped") == 1
        assert db.execute("SELECT resolved FROM doc_comments WHERE id=?", (plain[0]["id"],)).fetchone()[0] == 1

        comments.update(plain[0]["id"], {"resolved": True}, db=db)

        assert _description_and_events(db, node_id) == (description, events)
    finally:
        db.close()


def test_a_task_sent_back_settles_through_its_live_revision(isolated_db):
    db = get_db()
    try:
        _, suggested, plain, node_id, _ = _delivered(db, suggestions=1, comments_=1)
        submit_review_evidence(node_id, db=db, actor_id="owner")
        _, revision, _ = rejection.reject_submission(node_id, feedback="Cover the rest.", actor_id="parent", db=db)
        db.commit()
        assert _state(db, revision.id) == "pending"

        _decide(db, suggested[0])
        comments.update(plain[0]["id"], {"resolved": True}, db=db)

        assert _state(db, revision.id) == "accepted"
        assert _state(db, node_id) == "closed"
    finally:
        db.close()


def test_the_owners_active_current_task_is_accepted_and_frees_the_slot(isolated_db):
    db = get_db()
    try:
        _, _, plain, node_id, _ = _delivered(db, suggestions=0, comments_=1)
        admission.make_current(node_id, expected_revision=service._node_row(db, node_id)["revision"],
                               actor_id="owner", provenance="test", db=db)
        db.commit()
        assert [row["node_id"] for row in admission_attempts.current_for_executor(db, "owner")] == [node_id]

        comments.update(plain[0]["id"], {"resolved": True}, db=db)

        assert _state(db, node_id) == "accepted"
        assert admission_attempts.current_for_executor(db, "owner") == []
    finally:
        db.close()


def test_a_batch_a_person_left_is_not_an_agents_to_settle(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact_id = seed_document(db, "owner", name="Person.md")
        comment, _ = add_comment(db, artifact_id, author_kind="user", author_id=None, author="Robin")
        delivered = batches(db, state="delivered")[0]
        assert delivered["delivered_node_id"]

        comments.update(comment["id"], {"resolved": True}, db=db)

        assert _state(db, delivered["delivered_node_id"]) != "accepted"
    finally:
        db.close()


def test_a_batch_that_was_never_delivered_to_a_task_settles_nothing(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner", name="Open.md")
        suggestion, _ = add_comment(db, artifact_id, replacement="New")

        _decide(db, suggestion)

        assert batches(db)[0]["state"] == "open"
        assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 0
    finally:
        db.close()
