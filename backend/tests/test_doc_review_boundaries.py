"""N8a scope, retry, race and after-commit boundaries."""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from doc_feedback_helpers import add_comment, seed_thread
from doceditor import review_feedback
from infra.database import write_transaction
from plans import public
from test_doc_review_requests import request, review_env, stored, fork_comment  # noqa: F401


def test_create_settle_cancel_emit_one_artifact_frame_after_commit_and_none_on_rollback(review_env, monkeypatch):
    db, aid = review_env
    from infra import state
    frames = []
    def broadcast(user, frame):
        if frame["type"] == "artifacts_changed" and frame["artifact_id"] == aid:
            assert not db.in_transaction
            frames.append(frame)
    monkeypatch.setattr(state, "broadcast_to_user", broadcast)
    first = request(db, aid)
    assert len(frames) == 1
    fork_comment(db, aid, first["id"])
    with write_transaction(db, scope="test_review_settle"):
        review_feedback.submit_fork_feedback(db, first["id"], "reviewer", aid)
    assert len(frames) == 2
    second = request(db, aid, reviewer="second", key="second")
    assert len(frames) == 3
    from doceditor import review_requests as reviews
    actor = reviews.ReviewActor("agent", "requester", "requester", "owner-token", "Requester")
    reviews.cancel(db, second["id"], actor)
    assert len(frames) == 4
    with pytest.raises(RuntimeError, match="rollback"):
        with write_transaction(db, scope="test_review_rollback"):
            request(db, aid, key="rolled-back")
            raise RuntimeError("rollback")
    assert len(frames) == 4
    assert stored(db, first["id"])["state"] == "submitted"
    third = request(db, aid, key="settlement-rollback")
    assert len(frames) == 5
    with pytest.raises(RuntimeError, match="rollback"):
        with write_transaction(db, scope="test_settlement_rollback"):
            review_feedback.submit_fork_feedback(db, third["id"], "reviewer", aid)
            raise RuntimeError("rollback")
    assert len(frames) == 5
    assert stored(db, third["id"])["state"] == "forked"
    assert third["node_id"] is None
    with pytest.raises(RuntimeError, match="rollback"):
        with write_transaction(db, scope="test_cancel_rollback"):
            reviews.cancel(db, third["id"], actor)
            raise RuntimeError("rollback")
    assert len(frames) == 5
    assert stored(db, third["id"])["state"] == "forked"
    assert third["node_id"] is None


@pytest.mark.parametrize("lifecycle", ["archived", "purged", "deleted"])
def test_reviewer_gone_before_commit_rolls_back_request_and_node(review_env, monkeypatch, lifecycle):
    db, aid = review_env
    from doceditor import review_requests as reviews
    original = reviews._reviewer
    calls = []
    def disappear(connection, artifact, reviewer, actor):
        calls.append(reviewer)
        if len(calls) == 2:
            if lifecycle == "deleted":
                connection.execute("DELETE FROM threads WHERE id=?", (reviewer,))
            else:
                connection.execute("UPDATE threads SET lifecycle_state=?,archived_reason='user' WHERE id=?", (lifecycle, reviewer))
        return original(connection, artifact, reviewer, actor)
    monkeypatch.setattr(reviews, "_reviewer", disappear)
    with pytest.raises(reviews.ReviewRequestError):
        request(db, aid)
    assert db.execute("SELECT COUNT(*) FROM doc_review_requests").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 0


def test_concurrent_first_requests_share_one_request_node_and_append_once(review_env):
    db, aid = review_env
    path = db.execute("PRAGMA database_list").fetchone()[2]
    barrier = Barrier(2)
    def create(key):
        connection = sqlite3.connect(path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            barrier.wait(timeout=10)
            return request(connection, aid, key=key, note=None)
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ("first-key", "second-key")))
    assert results[0]["id"] == results[1]["id"]
    assert db.execute("SELECT COUNT(*) FROM doc_review_requests").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 0
    assert results[0]["fork_id"] == results[1]["fork_id"]


def test_wrong_recipient_batch_and_cancelled_request_do_not_settle(review_env):
    db, aid = review_env
    from doceditor import review_requests as reviews
    from doc_feedback_helpers import batches
    first = request(db, aid)
    add_comment(db, aid, author_id="second")
    root = db.execute("SELECT id FROM doc_comments LIMIT 1").fetchone()[0]
    db.execute("UPDATE threads SET lifecycle_state='archived',archived_reason='user' WHERE id='owner'")
    db.commit()
    add_comment(db, aid, author_id="reviewer", parent_id=root, submit=True)
    assert any(b["recipient_id"] == "second" and b["state"] == "delivered" for b in batches(db))
    assert stored(db, first["id"])["state"] == "forked"
    actor = reviews.ReviewActor("agent", "requester", "requester", "owner-token", "Requester")
    reviews.cancel(db, first["id"], actor)
    assert stored(db, first["id"])["state"] == "withdrawn"


def test_review_requested_node_accepts_and_terminal_node_cancel_preserves_verdict(review_env):
    db, aid = review_env
    from doceditor import review_requests as reviews
    from plans import review_evidence
    row = reviews.escalate_fork(db, request(db, aid)["id"])
    with write_transaction(db, scope="test_review_requested"):
        review_evidence.submit(row["node_id"], evidence="Comments ready", expected_revision=None, actor_id=None, provenance="test", db=db)
    add_comment(db, aid, author_id="reviewer", submit=True)
    assert public.get_node(row["node_id"], db=db).state == "accepted"
    actor = reviews.ReviewActor("agent", "requester", "requester", "owner-token", "Requester")
    assert reviews.cancel(db, row["id"], actor)["state"] == "submitted"
    assert public.get_node(row["node_id"], db=db).state == "accepted"


def test_foreign_artifact_note_limit_and_unauthorized_cancel_refuse(review_env):
    db, aid = review_env
    from doceditor import review_requests as reviews
    with pytest.raises(reviews.ReviewRequestError) as caught:
        request(db, aid, note="x" * 501)
    assert str(caught.value) == reviews.NOTE_REFUSAL.format(length=501)
    row = request(db, aid)
    actor = reviews.ReviewActor("agent", "second", "second", "owner-token", "Second")
    with pytest.raises(PermissionError) as caught:
        reviews.cancel(db, row["id"], actor)
    assert str(caught.value) == reviews.CANCEL_REFUSAL
    db.execute("UPDATE threads SET user_id='foreign' WHERE id='owner'")
    db.commit()
    with pytest.raises(PermissionError) as caught:
        request(db, aid, key="foreign-key")
    assert str(caught.value) == reviews.ARTIFACT_REFUSAL
    assert stored(db, row["id"])["state"] == "forked"


@pytest.mark.parametrize("owner", ["ui", "project:test"])
def test_sentinel_cancel_requires_original_requester_even_for_users(review_env, owner):
    from doc_feedback_helpers import seed_document
    from doceditor import review_requests as reviews
    db, _aid = review_env
    aid = seed_document(db, owner)
    row = request(db, aid, kind="user")
    foreign = reviews.ReviewActor("user", "another-user", "", "another-user", "Another user")
    with pytest.raises(PermissionError) as caught:
        reviews.cancel(db, row["id"], foreign)
    assert str(caught.value) == reviews.CANCEL_REFUSAL
    assert stored(db, row["id"])["state"] == "forked"
    requester = reviews.ReviewActor("user", "owner-token", "", "owner-token", "Steve")
    assert reviews.cancel(db, row["id"], requester)["state"] == "withdrawn"


@pytest.mark.parametrize("kind", ["agent", "user"])
def test_real_document_owner_can_cancel_and_foreign_user_cannot(review_env, kind):
    from doceditor import review_requests as reviews
    db, aid = review_env
    row = request(db, aid)
    foreign = reviews.ReviewActor("user", "another-user", "", "another-user", "Another user")
    with pytest.raises(PermissionError) as caught:
        reviews.cancel(db, row["id"], foreign)
    assert str(caught.value) == reviews.ARTIFACT_REFUSAL
    assert stored(db, row["id"])["state"] == "forked"
    owner = reviews.ReviewActor(kind, "owner" if kind == "agent" else "owner-token", "owner", "owner-token", "Owner")
    assert reviews.cancel(db, row["id"], owner)["state"] == "withdrawn"


def test_same_user_cross_project_reviewer_is_not_visible(review_env):
    from doceditor import review_requests as reviews
    db, aid = review_env
    db.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES ('other','Other project','t0','t0')")
    db.execute("UPDATE threads SET project_id='other' WHERE id='reviewer'")
    db.commit()
    with pytest.raises(reviews.ReviewRequestError) as caught:
        request(db, aid)
    assert str(caught.value) == reviews.ROSTER_REFUSAL.format(id="reviewer")
    assert db.execute("SELECT COUNT(*) FROM doc_review_requests").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 0
