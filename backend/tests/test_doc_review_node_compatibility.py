"""Existing review-node behavior remains available after a fork escalates."""

import json

import pytest

from doc_feedback_helpers import add_comment, batches, seed_thread
from doceditor import feedback_batches, review_lifecycle, review_requests as reviews
from infra.database import write_transaction
from plans import public
from test_doc_review_requests import request as fork_request, review_env, stored


def request(db, aid, **kwargs):
    row = fork_request(db, aid, **kwargs)
    if row["node_id"] is None:
        row = reviews.escalate_fork(db, row["id"])
    return row


@pytest.mark.parametrize("target", ["owner", "unlisted", "nonagent", "cross-scope"])
def test_owner_and_nonvisible_reviewers_are_refused_without_writes(review_env, target):
    db, aid = review_env
    if target in {"nonagent", "cross-scope"}:
        seed_thread(db, target)
        if target == "cross-scope":
            db.execute("UPDATE threads SET user_id='foreign' WHERE id=?", (target,))
        db.commit()
    with pytest.raises(reviews.ReviewRequestError) as caught:
        fork_request(db, aid, reviewer=target)
    expected = reviews.OWNER_REFUSAL if target == "owner" else reviews.ROSTER_REFUSAL.format(id=target)
    assert str(caught.value) == expected
    assert db.execute("SELECT COUNT(*) FROM doc_review_requests").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 0


def test_escalated_repeat_notes_keep_provenance_and_idempotency(review_env):
    db, aid = review_env
    first = request(db, aid)
    request(db, aid, note="Check evidence", key="second-key")
    request(db, aid, note="Check evidence", key="second-key")
    node = public.get_node(first["node_id"], db=db)
    assert node.description.count("Requester's note (context from Requester,") == 2
    assert node.description.count("Check evidence") == 1
    assert json.loads(stored(db, first["id"])["client_ids"]) == ["review-key", "second-key"]
    assert db.execute("SELECT COUNT(*) FROM doc_review_requests").fetchone()[0] == 1


def test_delivered_batch_settles_once_and_selects_next_task(review_env):
    db, aid = review_env
    result = request(db, aid)
    node = public.get_node(result["node_id"], db=db)
    next_node = public.add_node(node.plan_id, kind="task", title="Next task", acceptance_criteria="Done", db=db)
    public.transition_node(next_node.id, "active", db=db)
    db.commit()
    add_comment(db, aid, author_id="reviewer", submit=True)
    batch = batches(db)[0]
    assert stored(db, result["id"])["answered_batch_id"] == batch["id"]
    assert public.get_node(node.id, db=db).state == "accepted"
    evidence = public.latest_review_evidence(node.id, requirements_revision=node.requirements_revision, db=db)
    assert batch["id"] in evidence["evidence"]
    assert db.execute("SELECT released_at FROM plan_node_attempts WHERE node_id=?", (node.id,)).fetchone()[0]
    assert db.execute("SELECT node_id FROM plan_node_attempts WHERE released_at IS NULL AND executor_thread_id='reviewer'").fetchone()[0] == next_node.id
    with write_transaction(db, scope="test_repeat_settle"):
        review_lifecycle.settle_for_batch(db, batch)
    assert db.execute("SELECT COUNT(*) FROM plan_node_events WHERE node_id=? AND new_state='accepted'", (node.id,)).fetchone()[0] == 1


def test_undelivered_batch_stays_open_then_retry_settles(review_env):
    db, aid = review_env
    result = request(db, aid)
    db.execute("UPDATE threads SET lifecycle_state='archived',archived_reason='user' WHERE id='owner'")
    db.commit()
    add_comment(db, aid, author_id="reviewer", submit=True)
    assert batches(db)[0]["state"] == "undelivered"
    assert stored(db, result["id"])["state"] == "handed_back"
    db.execute("UPDATE threads SET lifecycle_state='active' WHERE id='owner'")
    db.commit()
    feedback_batches.retry_batch(db, batches(db)[0]["id"])
    assert stored(db, result["id"])["state"] == "submitted"
    assert public.get_node(result["node_id"], db=db).state == "accepted"


def test_nonterminal_settlement_failure_rolls_back_delivery_and_request(review_env, monkeypatch):
    db, aid = review_env
    result = request(db, aid)
    add_comment(db, aid, author_id="reviewer")
    batch_id = batches(db)[0]["id"]

    def fail(*args, **kwargs):
        raise RuntimeError("injected settlement failure")

    monkeypatch.setattr(public, "settle_review_node", fail)
    with pytest.raises(RuntimeError, match="injected settlement failure"):
        feedback_batches.close_batch(db, batch_id, "submit")
    assert batches(db)[0]["state"] == "open"
    assert stored(db, result["id"])["answered_batch_id"] is None
    assert public.get_node(result["node_id"], db=db).state == "active"
    assert db.execute("SELECT COUNT(*) FROM plan_node_artifact_links WHERE provenance='document.feedback_batch'").fetchone()[0] == 0
