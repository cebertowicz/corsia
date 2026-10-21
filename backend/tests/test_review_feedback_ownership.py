"""Fork comments share ordinary feedback from creation."""

import pytest

from doceditor import comments, feedback_batches, feedback_state, review_feedback, review_requests
from infra.database import write_transaction
from plans import public
from test_doc_review_requests import request, review_env, fork_comment, stored  # noqa: F401
from tools.document_feedback import _send_feedback


def ordinary_comment(db, aid):
    with write_transaction(db, scope="test_continuation_comment"):
        comment, created = comments.create_with_status(
            aid, body="The continuation adds evidence.", author="Reviewer", author_kind="agent",
            author_thread_id="reviewer", review=True, client_id=f"continuation-{aid}", db=db,
        )
        return feedback_batches.record_comment(db, comment, created=created)["batch_ids"][0]


def submit(aid):
    return _send_feedback("reviewer", {"user_id": "owner-token"}, aid, None)


def test_continuation_adds_one_comment_produces_one_feedback_task_with_all_findings(review_env):
    db, aid = review_env
    row = request(db, aid)
    fork_comment(db, aid, row["id"])
    continuation = review_requests.escalate_fork(db, row["id"])
    ordinary = ordinary_comment(db, aid)

    with write_transaction(db, scope="test_continuation_turn_end"):
        feedback_batches.close_for_turn_end(db, "reviewer")

    assert stored(db, row["id"])["state"] == "submitted"
    assert stored(db, row["id"])["answered_batch_id"] == ordinary
    assert not public.review_is_open(db, continuation["node_id"])
    tasks = db.execute("SELECT n.description FROM plan_nodes n JOIN plans p ON p.id=n.plan_id WHERE p.owner_kind='thread' AND p.owner_id='owner'").fetchall()
    assert len(tasks) == 1
    assert "The claim needs evidence." in tasks[0]["description"]
    assert "The continuation adds evidence." in tasks[0]["description"]
    assert db.execute("SELECT COUNT(*) FROM doc_feedback_batches").fetchone()[0] == 1
    assert db.execute("SELECT item_count FROM doc_feedback_batches WHERE id=?", (ordinary,)).fetchone()[0] == 2


def test_empty_continuation_submit_delivers_existing_feedback_once(review_env):
    db, aid = review_env
    row = request(db, aid)
    fork_comment(db, aid, row["id"])
    continuation = review_requests.escalate_fork(db, row["id"])

    submit(aid)
    submit(aid)

    assert stored(db, row["id"])["state"] == "submitted"
    assert stored(db, row["id"])["answered_batch_id"]
    assert not public.review_is_open(db, continuation["node_id"])
    assert db.execute("SELECT COUNT(*) FROM plan_nodes n JOIN plans p ON p.id=n.plan_id WHERE p.owner_kind='thread' AND p.owner_id='owner'").fetchone()[0] == 1


def test_ended_continuation_quiet_closer_delivers_all_ordinary_comments(review_env):
    db, aid = review_env
    row = request(db, aid)
    fork_comment(db, aid, row["id"])
    continuation = review_requests.escalate_fork(db, row["id"])
    with write_transaction(db, scope="test_end_without_submit"):
        public.close_review_node(db, continuation["node_id"], reason="No longer needed")
    ordinary_comment(db, aid)

    feedback_batches.close_due("9999-01-01T00:00:00+00:00", db=db)

    assert stored(db, row["id"])["state"] == "handed_back"
    assert stored(db, row["id"])["answered_batch_id"] is None
    assert db.execute("SELECT COUNT(*) FROM doc_feedback_batch_items").fetchone()[0] >= 1
    batch = db.execute("SELECT state FROM doc_feedback_batches").fetchone()
    assert batch["state"] == "delivered"
    tasks = db.execute("SELECT n.description FROM plan_nodes n JOIN plans p ON p.id=n.plan_id WHERE p.owner_kind='thread' AND p.owner_id='owner'").fetchall()
    assert len(tasks) == 1
    assert "The continuation adds evidence." in tasks[0]["description"]
    assert "The claim needs evidence." in tasks[0]["description"]


@pytest.mark.parametrize("state", ["forked", "handed_back", "failed"])
def test_generic_closers_deliver_fork_comments_in_every_open_request_state(review_env, state):
    db, aid = review_env
    row = request(db, aid)
    fork_comment(db, aid, row["id"])
    if state != "forked":
        review_requests.escalate_fork(db, row["id"], reason="limit" if state == "handed_back" else "target_unavailable")
    with write_transaction(db, scope="test_generic_closers"):
        feedback_batches.close_for_document(db, author_kind="agent", author_id="reviewer", artifact_id=aid, reason="submit")
        assert feedback_batches.close_for_turn_end(db, "reviewer") == []
    assert feedback_batches.close_due("9999-01-01T00:00:00+00:00", db=db) == []
    batch = db.execute("SELECT state FROM doc_feedback_batches").fetchone()
    assert batch["state"] == "delivered"
    assert stored(db, row["id"])["answered_batch_id"]
    assert stored(db, row["id"])["state"] == "submitted"


def test_active_fork_feedback_uses_ordinary_pending_notices_and_retry(review_env, monkeypatch):
    from doceditor import feedback_delivery

    db, aid = review_env
    row = request(db, aid)
    fork_comment(db, aid, row["id"])
    identity = dict(author_kind="agent", author_id="reviewer", artifact_id=aid)
    original = feedback_delivery.recipient_problem
    monkeypatch.setattr(feedback_delivery, "recipient_problem", lambda *args: "owner_unavailable")
    with write_transaction(db, scope="test_ordinary_undelivered"):
        feedback_batches.close_for_document(db, **identity, reason="submit")
        assert feedback_state.has_pending_for_document(db, **identity)
        assert len(feedback_state.undelivered_for_document(db, **identity)) == 1
        assert len(feedback_state.take_unreported_undelivered(db, "reviewer", "now")) == 1
    monkeypatch.setattr(feedback_delivery, "recipient_problem", original)
    with write_transaction(db, scope="test_ordinary_retry"):
        feedback_batches.retry_for_document(db, **identity)
    assert db.execute("SELECT state FROM doc_feedback_batches").fetchone()[0] == "delivered"
    assert stored(db, row["id"])["state"] == "submitted"
    assert len(comments.list_for_artifact(aid, db=db)) == 1


def test_raising_due_batch_rolls_back_alone(review_env, monkeypatch):
    db, aid = review_env
    ordinary = ordinary_comment(db, aid)
    from doc_feedback_helpers import seed_document
    other = seed_document(db, "owner", name="other.md")
    later = ordinary_comment(db, other)
    original = feedback_batches.close_batch

    def close(connection, batch_id, reason):
        if batch_id == ordinary:
            with write_transaction(connection, scope="test_failed_batch"):
                connection.execute("UPDATE doc_feedback_batches SET item_count=123 WHERE id=?", (batch_id,))
                raise RuntimeError("one broken batch")
        return original(connection, batch_id, reason)

    monkeypatch.setattr(feedback_batches, "close_batch", close)
    feedback_batches.close_due("9999-01-01T00:00:00+00:00", db=db)
    assert db.execute("SELECT state,item_count FROM doc_feedback_batches WHERE id=?", (ordinary,)).fetchone()[:] == ("open", 1)
    assert db.execute("SELECT state FROM doc_feedback_batches WHERE id=?", (later,)).fetchone()[0] == "delivered"


def test_submitted_fork_failure_is_noticed_and_retry_delivers_ordinary_batch(review_env, monkeypatch):
    from doceditor import feedback_delivery
    db, aid = review_env
    row = request(db, aid)
    fork_comment(db, aid, row["id"])
    original = feedback_delivery.recipient_problem
    monkeypatch.setattr(feedback_delivery, "recipient_problem", lambda *args: "owner_unavailable")
    with write_transaction(db, scope="test_submit_delivery_failure"):
        pending, woken, failures = review_feedback.submit_fork_feedback(db, row["id"], "reviewer", aid)
    assert pending and failures and not woken
    assert stored(db, row["id"])["state"] == "forked"
    batch = db.execute("SELECT * FROM doc_feedback_batches").fetchone()
    assert batch["state"] == "undelivered"
    attempts = batch["attempts"]
    assert stored(db, row["id"])["answered_batch_id"] is None
    assert db.execute("SELECT COUNT(*) FROM doc_feedback_batch_items").fetchone()[0] >= 1
    kwargs = dict(author_kind="agent", author_id="reviewer", artifact_id=aid)
    assert feedback_state.undelivered_for_document(db, **kwargs)[0]["id"] == batch["id"]
    with write_transaction(db, scope="test_failure_notice_and_retry"):
        assert feedback_state.take_unreported_undelivered(db, "reviewer", "now")[0]["id"] == batch["id"]
        monkeypatch.setattr(feedback_delivery, "recipient_problem", original)
        feedback_batches.retry_for_document(db, **kwargs)
    assert db.execute("SELECT state,attempts FROM doc_feedback_batches WHERE id=?", (batch["id"],)).fetchone()[:] == ("delivered", attempts + 1)
    assert stored(db, row["id"])["state"] == "submitted"
    assert stored(db, row["id"])["answered_batch_id"] == batch["id"]
    assert feedback_state.undelivered_for_document(db, **kwargs) == []


@pytest.mark.parametrize("state", ["submitted", "withdrawn", "handed_back", "failed"])
def test_state_event_is_emitted_once_only_after_commit(review_env, monkeypatch, state):
    from doceditor.review_lifecycle import transition
    from infra import product_events
    db, aid = review_env
    row = request(db, aid)
    events = []
    monkeypatch.setattr(product_events, "emit_product_event", lambda *args, **kwargs: events.append((args, kwargs)))
    reason = "limit" if state == "handed_back" else "target_unavailable" if state == "failed" else None
    with write_transaction(db, scope="test_review_outcome_event"):
        assert transition(db, row["id"], state, reason=reason).changed
        assert not events
    with write_transaction(db, scope="test_repeated_review_outcome"):
        assert not transition(db, row["id"], state, reason=reason).changed
    assert len(events) == 1
    assert events[0][0] == ("documents", f"review_{state}")
    assert events[0][1]["properties"] == {"request_id": row["id"], "reason": reason}


def test_rollback_leaves_no_state_event_or_feedback_change(review_env, monkeypatch):
    from doceditor.review_lifecycle import transition
    from infra import product_events
    db, aid = review_env
    row = request(db, aid)
    fork_comment(db, aid, row["id"])
    events = []
    monkeypatch.setattr(product_events, "emit_product_event", lambda *args, **kwargs: events.append(args))
    with pytest.raises(RuntimeError, match="abort"):
        with write_transaction(db, scope="test_abort_review_outcome"):
            transition(db, row["id"], "withdrawn")
            raise RuntimeError("abort")
    assert not events
    assert stored(db, row["id"])["state"] == "forked"
    assert db.execute("SELECT item_count,state FROM doc_feedback_batches").fetchone()[:] == (1, "open")


def test_coalescing_rolls_back_with_a_raising_delivery(review_env, monkeypatch):
    from doceditor import feedback_delivery
    db, aid = review_env
    row = request(db, aid)
    fork_comment(db, aid, row["id"])
    review_requests.escalate_fork(db, row["id"])
    ordinary_comment(db, aid)
    before = [tuple(batch) for batch in db.execute("SELECT id,state,item_count FROM doc_feedback_batches ORDER BY id")]

    def broken(*args):
        raise RuntimeError("delivery failed")

    monkeypatch.setattr(feedback_delivery, "deliver_claimed", broken)
    with pytest.raises(RuntimeError, match="delivery failed"):
        submit(aid)
    assert [tuple(batch) for batch in db.execute("SELECT id,state,item_count FROM doc_feedback_batches ORDER BY id")] == before
    assert db.execute("SELECT COUNT(*) FROM doc_feedback_batch_items").fetchone()[0] == 2
    assert stored(db, row["id"])["state"] == "handed_back"
