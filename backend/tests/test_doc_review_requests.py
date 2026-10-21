"""Fork-first document review lifecycle and feedback-isolation tests."""
from __future__ import annotations

import json

import pytest

from doc_feedback_helpers import seed_document, seed_thread
from doceditor import comments, feedback_batches, review_feedback
from infra.database import get_db, write_transaction
from infra.time import utc_now_iso
from plans import public


@pytest.fixture
def review_env(isolated_db, monkeypatch):
    from doceditor import live_runtime

    # Review-fork tests exercise durable background writes, not a live editor's
    # owner-loop path; a manager retained by an unrelated prior test is stale.
    monkeypatch.setattr(live_runtime, "_manager", None)
    db = get_db()
    for thread_id in ("owner", "requester", "reviewer", "second"):
        seed_thread(db, thread_id, title=thread_id.title())
        db.execute(
            "UPDATE threads SET agent_enabled=1,agent_responsibility='Review documents' WHERE id=?",
            (thread_id,),
        )
    artifact_id = seed_document(db, "owner", name="PRD.md")
    yield db, artifact_id
    db.close()


def request(db, artifact_id, *, kind="agent", reviewer="reviewer", note="Check claims", key="review-key"):
    from doceditor import review_feedback, review_requests

    actor = review_requests.ReviewActor(
        kind, "owner-token" if kind == "user" else "requester", "requester", "owner-token",
        "Reviewer requester" if kind == "user" else "Requester",
    )
    return review_requests.request_review(db, artifact_id, reviewer, actor, note, key)


def stored(db, request_id):
    return dict(db.execute("SELECT * FROM doc_review_requests WHERE id=?", (request_id,)).fetchone())


def staged_comment(db, artifact_id, request_id, *, submit=False):
    with write_transaction(db, scope="test_fork_review_comment") as connection:
        row, created = comments.create_with_status(
            artifact_id, body="The claim needs evidence.", author="Reviewer",
            author_kind="agent", author_thread_id="reviewer", review=True,
            client_id=f"comment-{request_id}", db=connection,
        )
        feedback_batches.record_comment(
            connection, row, created=created, review_request_id=request_id, submit=submit,
        )


def reviewer_turn(db, started_at):
    """Start the reviewer's next Turn at ``started_at``, ending any running one."""
    with write_transaction(db, scope="test_reviewer_turn") as connection:
        connection.execute(
            "UPDATE active_turns SET status='completed',ended_at=? WHERE thread_id='reviewer'"
            " AND status='running'", (started_at,),
        )
        connection.execute(
            """INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
               VALUES (?,'reviewer',0,'test-model','running',?,'boot')""",
            (f"turn-{started_at}", started_at),
        )


def close_reviewer_turn(db):
    with write_transaction(db, scope="test_reviewer_turn_end") as connection:
        return feedback_batches.close_for_turn_end(connection, "reviewer")


def test_request_is_node_less_and_persists_a_running_fork(review_env):
    db, artifact_id = review_env

    result = request(db, artifact_id)

    assert result["fork_id"] and result["node_id"] is None
    assert result["state"] == "forked"
    assert "requesting_node_id" not in result and "requirements_revision" not in result
    assert result["outcome"] == "open"
    assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 0


def test_same_key_is_idempotent_but_a_late_new_note_is_refused(review_env):
    from doceditor import review_feedback, review_requests

    db, artifact_id = review_env
    first = request(db, artifact_id)

    assert request(db, artifact_id) == first
    with pytest.raises(review_requests.ReviewRequestError, match="already in progress"):
        request(db, artifact_id, note="Check evidence", key="second-key")
    assert json.loads(stored(db, first["id"])["client_ids"]) == ["review-key"]


def test_submit_settles_node_less_request_and_leaves_reviewer_unselected(review_env):
    from doceditor import review_feedback, review_requests

    db, artifact_id = review_env
    result = request(db, artifact_id)
    staged_comment(db, artifact_id, result["id"])

    with write_transaction(db, scope="test_submit_fork_feedback"):
        pending, woken, undelivered = review_feedback.submit_fork_feedback(
            db, result["id"], "reviewer", artifact_id,
        )

    assert pending and not undelivered
    assert stored(db, result["id"])["answered_batch_id"]
    assert review_requests.outcome(db, stored(db, result["id"])) == "commented"
    assert "reviewer" not in woken
    assert db.execute("SELECT COUNT(*) FROM plan_nodes WHERE owner_thread_id='reviewer'").fetchone()[0] == 0


def test_withdrawal_keeps_seen_and_replied_comments_open_without_delivering(review_env, monkeypatch):
    """A comment people already saw and answered is never deleted, resolved or agreed to."""
    from doceditor import comment_notifications, review_requests

    db, artifact_id = review_env
    first = request(db, artifact_id)
    staged_comment(db, artifact_id, first["id"])
    root = db.execute("SELECT id FROM doc_comments WHERE body='The claim needs evidence.'").fetchone()["id"]
    reply = comments.create(artifact_id, body="Which claim?", author="Steve", parent_id=root, db=db)
    with write_transaction(db, scope="test_thread_view") as connection:
        connection.execute(
            "INSERT INTO doc_comment_thread_views (user_id,root_id,seen_at,seen_reply_id) VALUES (?,?,?,?)",
            ("owner-token", root, reply["created_at"], reply["id"]),
        )
    changed = []
    monkeypatch.setattr(
        comment_notifications, "announce_after_commit",
        lambda _connection, changed_artifact: changed.append(changed_artifact),
    )
    actor = review_requests.ReviewActor("agent", "requester", "requester", "owner-token", "Requester")

    assert review_requests.cancel(db, first["id"], actor)["outcome"] == "cancelled"

    kept = db.execute("SELECT resolved, acked FROM doc_comments WHERE id=?", (root,)).fetchone()
    assert kept is not None and kept["resolved"] == 0 and kept["acked"] == 0
    assert db.execute("SELECT COUNT(*) FROM doc_comments WHERE id=?", (reply["id"],)).fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM doc_comment_thread_views WHERE root_id=?", (root,)).fetchone()[0] == 1
    assert changed == []  # no comment write, so no comment-change announcement
    assert db.execute(
        "SELECT COUNT(*) FROM doc_feedback_batches WHERE review_request_id=?", (first["id"],),
    ).fetchone()[0] == 0
    assert stored(db, first["id"])["answered_batch_id"] is None


def test_withdrawal_drops_only_this_forks_undelivered_batches(review_env):
    from doceditor import review_feedback, review_requests

    db, artifact_id = review_env
    first = request(db, artifact_id)
    staged_comment(db, artifact_id, first["id"])
    # An unrelated reviewer batch must survive a fork withdrawal.
    with write_transaction(db, scope="test_unrelated_comment") as connection:
        row, created = comments.create_with_status(
            artifact_id, body="Unrelated feedback.", author="Reviewer", author_kind="agent",
            author_thread_id="reviewer", review=True, client_id="unrelated", db=connection,
        )
        feedback_batches.record_comment(connection, row, created=created)
    actor = review_requests.ReviewActor("agent", "requester", "requester", "owner-token", "Requester")

    cancelled = review_requests.cancel(db, first["id"], actor)

    assert cancelled["outcome"] == "cancelled"
    assert db.execute("SELECT resolved FROM doc_comments WHERE body='The claim needs evidence.'").fetchone()[0] == 0
    assert db.execute("SELECT resolved FROM doc_comments WHERE body='Unrelated feedback.'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM doc_comments WHERE body='Unrelated feedback.'").fetchone()[0] == 1
    assert db.execute(
        "SELECT COUNT(*) FROM doc_feedback_batches WHERE review_request_id=?", (first["id"],),
    ).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 0


def test_terminal_fork_escalates_once_to_the_existing_review_node(review_env):
    from doceditor import review_feedback, review_requests

    db, artifact_id = review_env
    result = request(db, artifact_id)

    escalated = review_requests.escalate_fork(db, result["id"])

    assert escalated and escalated["node_id"]
    assert escalated["fork_id"] is None
    assert escalated["state"] == "handed_back" and escalated["reason"] == "limit"
    assert public.get_node(escalated["node_id"], db=db).state == "active"
    assert review_requests.escalate_fork(db, result["id"]) is None


def test_escalation_preserves_comments_and_passes_requesting_intent(review_env, monkeypatch):
    from doceditor import review_feedback, review_requests

    db, artifact_id = review_env
    result = request(db, artifact_id)
    staged_comment(db, artifact_id, result["id"])
    seen = []
    create = public.create_review_node

    def capture(connection, reviewer, request_):
        seen.append(request_)
        return create(connection, reviewer, request_)

    monkeypatch.setattr(public, "create_review_node", capture)
    escalated = review_requests.escalate_fork(db, result["id"])
    comment = db.execute("SELECT id FROM doc_comments WHERE body='The claim needs evidence.'").fetchone()
    assert comment is not None
    assert "intent_node_id" not in seen[0] and "requesting_node_id" not in seen[0]
    node = public.get_node(escalated["node_id"], db=db)
    assert comment["id"] in node.description and "Check claims" in node.description
    assert db.execute("SELECT state FROM doc_feedback_batches WHERE review_request_id=?", (result["id"],)).fetchone()[0] == "open"
    reviewer_turn(db, utc_now_iso())
    with write_transaction(db, scope="test_continuation_submit"):
        from doceditor.review_lifecycle import submit_without_batch
        submit_without_batch(db, artifact_id, "reviewer")
    assert stored(db, result["id"])["answered_batch_id"]


def test_escalated_comments_wait_for_the_reviewers_own_continuation(review_env):
    """D2: neither the quiet timer nor a Turn already running delivers them."""
    from doceditor import review_feedback, review_requests

    db, artifact_id = review_env
    result = request(db, artifact_id)
    staged_comment(db, artifact_id, result["id"])
    reviewer_turn(db, utc_now_iso())  # running before the fork gives up
    escalated = review_requests.escalate_fork(db, result["id"])

    feedback_batches.close_due("9999-12-31T00:00:00+00:00", db=db)
    close_reviewer_turn(db)

    batch = db.execute(
        "SELECT state FROM doc_feedback_batches WHERE review_request_id=?", (result["id"],),
    ).fetchone()
    assert batch["state"] == "open"
    assert stored(db, result["id"])["answered_batch_id"] is None
    assert public.review_is_open(db, escalated["node_id"])

    reviewer_turn(db, utc_now_iso())
    with write_transaction(db, scope="test_continuation_submit"):
        from doceditor.review_lifecycle import submit_without_batch
        submit_without_batch(db, artifact_id, "reviewer")
    answered = stored(db, result["id"])["answered_batch_id"]
    assert answered
    assert db.execute("SELECT state FROM doc_feedback_batches WHERE id=?", (answered,)).fetchone()[0] == "delivered"
    assert not public.review_is_open(db, escalated["node_id"])
    reviewer_turn(db, utc_now_iso())
    assert close_reviewer_turn(db) == []
    assert stored(db, result["id"])["answered_batch_id"] == answered


def test_escalated_comments_deliver_on_the_reviewers_submit(review_env):
    """D2: the reviewer's own submit after escalation delivers and settles once."""
    from doceditor import review_feedback, review_requests

    db, artifact_id = review_env
    result = request(db, artifact_id)
    staged_comment(db, artifact_id, result["id"])
    escalated = review_requests.escalate_fork(db, result["id"])

    with write_transaction(db, scope="test_submit_escalated") as connection:
        from doceditor.review_lifecycle import submit_without_batch
        submit_without_batch(connection, artifact_id, "reviewer")

    assert stored(db, result["id"])["answered_batch_id"]
    assert not public.review_is_open(db, escalated["node_id"])


def test_clean_submit_ends_without_comments_and_never_escalates(review_env):
    """D4: a fork that submits nothing ends clean, with a receipt, and no node."""
    from doceditor import review_feedback, review_requests
    from core import artifact_review_capture

    db, artifact_id = review_env
    result = request(db, artifact_id)

    with write_transaction(db, scope="test_clean_submit"):
        pending, _woken, undelivered = review_feedback.submit_fork_feedback(
            db, result["id"], "reviewer", artifact_id,
        )

    assert not pending and not undelivered
    row = stored(db, result["id"])
    assert review_requests.outcome(db, row) == "ended_without_comments"
    assert row["state"] == "submitted"
    assert artifact_review_capture.submitted(result["id"])
    assert review_requests.escalate_fork(db, result["id"]) is None
    assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 0
    receipt = db.execute(
        "SELECT COUNT(*) FROM context_change_journal WHERE object_kind='thread_context'"
        " AND object_id='reviewer' AND summary LIKE '%no comments were left%'",
    ).fetchone()[0]
    assert receipt == 1


def test_schema_scopes_open_batches_by_review_request(review_env):
    db, artifact_id = review_env
    first = request(db, artifact_id)
    staged_comment(db, artifact_id, first["id"])
    batch = db.execute("SELECT review_request_id FROM doc_feedback_batches").fetchone()
    assert batch["review_request_id"] == first["id"]
