"""A person's delivery lane is always named by the caller: no path picks one for them."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auth_fixtures import OWNER_TOKEN, OWNER_USER
from doc_feedback_helpers import add_comment, batches, seed_document, seed_thread
from doceditor import comments, feedback_batch_edits, feedback_batches
from infra.database import get_db, write_transaction

LANES = ["immediate", "document-held"]


@pytest.fixture
def client(isolated_db, monkeypatch):
    from doceditor import live_runtime
    from main import app
    from plans import public

    async def drive_threads(_thread_ids):
        return None

    # This client runs no lifespan, so a manager left by an integration test's app has no loop here.
    monkeypatch.setattr(live_runtime, "_manager", None)
    monkeypatch.setattr(public, "drive_threads", drive_threads)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def artifact_id(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        db.execute("UPDATE threads SET user_id=? WHERE id='owner'", (OWNER_USER,))
        return seed_document(db, "owner")
    finally:
        db.close()


def _person_lanes() -> list[str]:
    db = get_db()
    try:
        return [batch["delivery_policy"] for batch in batches(db) if batch["author_kind"] == "user"]
    finally:
        db.close()


def _resolved_agent_comment(artifact_id: str) -> str:
    db = get_db()
    try:
        comment, _ = add_comment(db, artifact_id)
        return comments.update(comment["id"], {"resolved": True}, db=db)["id"]
    finally:
        db.close()


def _post(client, artifact_id: str, **fields):
    return client.post(
        f"/api/artifacts/{artifact_id}/comments", params={"token": OWNER_TOKEN},
        json={"body": "Please check this.", **fields},
    )


def _reopen(client, comment_id: str, **fields):
    return client.patch(
        f"/api/comments/{comment_id}", params={"token": OWNER_TOKEN}, json={"resolved": False, **fields},
    )


@pytest.mark.parametrize("lane", LANES)
def test_a_created_comment_stays_in_the_lane_it_was_given(client, artifact_id, lane):
    response = _post(client, artifact_id, delivery_policy=lane)

    assert response.status_code == 200, response.text
    assert response.json()["delivery_policy"] == lane
    assert _person_lanes() == [lane]


def test_a_created_comment_without_a_lane_is_refused_and_creates_nothing(client, artifact_id):
    response = _post(client, artifact_id)

    assert response.status_code == 422
    assert "delivery_policy" in response.text
    assert _person_lanes() == []
    listed = client.get(f"/api/artifacts/{artifact_id}/comments", params={"token": OWNER_TOKEN}).json()
    assert listed["comments"] == []


@pytest.mark.parametrize("lane", LANES)
def test_a_reopen_stays_in_the_lane_it_was_given(client, artifact_id, lane):
    comment_id = _resolved_agent_comment(artifact_id)

    response = _reopen(client, comment_id, delivery_policy=lane)

    assert response.status_code == 200, response.text
    assert _person_lanes() == [lane]


def test_a_reopen_without_a_lane_is_refused_and_leaves_the_comment_resolved(client, artifact_id):
    comment_id = _resolved_agent_comment(artifact_id)

    response = _reopen(client, comment_id)

    assert response.status_code == 422
    assert "delivery_policy" in response.text
    assert _person_lanes() == []
    db = get_db()
    try:
        assert comments.get_comment(comment_id, db=db)["resolved"] == 1
    finally:
        db.close()


def test_a_patch_that_is_not_a_reopen_needs_no_lane(client, artifact_id):
    comment_id = _resolved_agent_comment(artifact_id)

    resolved = client.patch(
        f"/api/comments/{comment_id}", params={"token": OWNER_TOKEN}, json={"acked": True},
    )

    assert resolved.status_code == 200, resolved.text
    assert _person_lanes() == []


@pytest.mark.parametrize("lane", LANES)
def test_record_reopen_keeps_the_lane_it_was_given(isolated_db, lane):
    db = get_db()
    try:
        seed_thread(db, "owner")
        comment, _ = add_comment(db, seed_document(db, "owner"))
        comment = comments.update(comment["id"], {"resolved": True}, db=db)
        with write_transaction(db, scope="test_reopen_lane") as connection:
            feedback_batch_edits.record_reopen(
                connection, comment, user_id="owner-token", user_display="Steve", delivery_policy=lane,
            )
        assert [batch["delivery_policy"] for batch in batches(db) if batch["author_kind"] == "user"] == [lane]
    finally:
        db.close()


def test_record_reopen_and_the_transition_have_no_default_lane(isolated_db):
    from doceditor.comment_feedback_transitions import apply_user_transition

    db = get_db()
    try:
        seed_thread(db, "owner")
        comment, _ = add_comment(db, seed_document(db, "owner"))
        comment = comments.update(comment["id"], {"resolved": True}, db=db)
        with write_transaction(db, scope="test_reopen_without_lane") as connection:
            with pytest.raises(TypeError):
                feedback_batch_edits.record_reopen(connection, comment, user_id="owner-token", user_display="Steve")
            with pytest.raises(TypeError):
                apply_user_transition(connection, comment, {"resolved": False}, {"id": "owner-token", "name": "Steve"})
            with pytest.raises(ValueError):
                apply_user_transition(
                    connection, comment, {"resolved": False}, {"id": "owner-token", "name": "Steve"}, None,
                )
        assert [batch for batch in batches(db) if batch["author_kind"] == "user"] == []
    finally:
        db.close()


def test_a_person_save_opens_an_immediate_batch_with_a_five_second_deadline(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact = seed_document(db, "owner")
        db.commit()
        with write_transaction(db, scope="test_edit_lane") as connection:
            feedback_batch_edits.record_edit(
                connection, artifact_id=artifact, recipient_id="owner", author_kind="user", author_id="ui",
                actor_display="Steve", path=Path("/workspace/shared/doc.md"),
                before=b"old\n", after=b"new\n",
            )
        [batch] = batches(db)
        assert batch["delivery_policy"] == feedback_batches.IMMEDIATE
        assert (datetime.fromisoformat(batch["closes_at"]) - datetime.fromisoformat(batch["last_item_at"])).total_seconds() == 5
    finally:
        db.close()
