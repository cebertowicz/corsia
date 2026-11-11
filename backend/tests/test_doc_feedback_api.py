"""HTTP contract for authenticated document-feedback actions."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from auth_fixtures import OTHER_TOKEN, OWNER_TOKEN, OWNER_USER
from doc_feedback_helpers import add_comment, batches, seed_document, seed_thread
from doceditor import feedback_batch_edits, feedback_batches
from infra.database import get_db, write_transaction


@pytest.fixture
def client(isolated_db):
    from main import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def artifact_id(isolated_db):
    db = get_db()
    try:
        return seed_document(db, "ui")
    finally:
        db.close()


def test_user_immediate_comment_waits_for_the_settle_then_is_delivered_once(client, isolated_db, monkeypatch):
    from doceditor import live_runtime
    from plans import public

    # This client runs no lifespan, so a manager left by an integration test's app has no loop here.
    monkeypatch.setattr(live_runtime, "_manager", None)
    db = get_db()
    try:
        seed_thread(db, "owner")
        db.execute("UPDATE threads SET user_id=? WHERE id='owner'", (OWNER_USER,))
        artifact_id = seed_document(db, "owner")
    finally:
        db.close()
    drives = []

    async def drive_threads(thread_ids):
        drives.append(list(thread_ids))

    monkeypatch.setattr(public, "drive_threads", drive_threads)
    payload = {"body": "Please change this.", "client_id": "immediate-comment", "delivery_policy": "immediate"}
    first = client.post(f"/api/artifacts/{artifact_id}/comments?token={OWNER_TOKEN}", json=payload)
    replay = client.post(f"/api/artifacts/{artifact_id}/comments?token={OWNER_TOKEN}", json=payload)

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert first.json()["woken"] == []
    assert replay.json()["woken"] == []
    db = get_db()
    try:
        [batch] = batches(db)
        assert (batch["state"], batch["item_count"]) == ("open", 1)
        assert drives == []
        assert feedback_batches.close_due(batch["closes_at"], db=db) == ["owner"]
        [batch] = batches(db)
        assert (batch["state"], batch["item_count"], batch["attempts"]) == ("delivered", 1, 1)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_comment_handler_returns_before_agent_delivery(isolated_db, monkeypatch):
    from api import doc_comments
    from api.doc_comment_models import CommentCreate
    from doceditor import live_runtime

    monkeypatch.setattr(live_runtime, "_manager", None)
    db = get_db()
    try:
        seed_thread(db, "owner")
        db.execute("UPDATE threads SET user_id=? WHERE id='owner'", (OWNER_USER,))
        created_artifact_id = seed_document(db, "owner")
    finally:
        db.close()
    result = await doc_comments.create_comment(
        created_artifact_id, OWNER_TOKEN,
        CommentCreate(body="Save before delivery.", client_id="blocked-delivery", delivery_policy="immediate"),
    )
    assert result["comment"]["id"] == "blocked-delivery"
    assert result["woken"] == []
    db = get_db()
    try:
        [batch] = batches(db)
        assert (batch["state"], batch["item_count"]) == ("open", 1)
    finally:
        db.close()


def test_held_user_comment_returns_before_delivery_and_keeps_its_policy_batch_open(client, isolated_db, monkeypatch):
    from doceditor import comment_notifications, live_runtime
    from plans import public

    monkeypatch.setattr(live_runtime, "_manager", None)
    db = get_db()
    try:
        seed_thread(db, "owner")
        db.execute("UPDATE threads SET user_id=? WHERE id='owner'", (OWNER_USER,))
        created_artifact_id = seed_document(db, "owner")
    finally:
        db.close()
    frames: list[str] = []
    drives: list[list[str]] = []
    monkeypatch.setattr(comment_notifications, "broadcast_doc_comment_changed", lambda artifact_id, **_kwargs: frames.append(artifact_id))

    async def drive_threads(thread_ids):
        drives.append(list(thread_ids))

    monkeypatch.setattr(public, "drive_threads", drive_threads)
    response = client.post(
        f"/api/artifacts/{created_artifact_id}/comments?token={OWNER_TOKEN}",
        json={"body": "Keep this with the document.", "client_id": "held-comment", "delivery_policy": "document-held"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["delivery_policy"] == "document-held"
    db = get_db()
    try:
        [batch] = batches(db)
        assert (batch["state"], batch["delivery_policy"], batch["attempts"]) == ("open", "document-held", 0)
    finally:
        db.close()
    assert frames == [created_artifact_id]
    assert drives == []
    listed = client.get(f"/api/artifacts/{created_artifact_id}/comments?token={OWNER_TOKEN}")
    assert listed.status_code == 200
    assert [comment["id"] for comment in listed.json()["comments"]] == ["held-comment"]


def test_held_comment_is_visible_to_another_reader_of_shared_document(client, artifact_id):
    url = f"/api/artifacts/{artifact_id}/comments"
    posted = client.post(url, params={"token": OWNER_TOKEN}, json={
        "body": "Shared held comment", "client_id": "shared-held", "delivery_policy": "document-held",
    })
    assert posted.status_code == 200, posted.text
    listed = client.get(url, params={"token": OTHER_TOKEN})
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()["comments"]] == ["shared-held"]


def test_no_recipient_person_comment_still_sends_one_frame(client, isolated_db, monkeypatch):
    """A user comment on their own document is announced once at commit."""
    from doceditor import comment_notifications, live_runtime

    monkeypatch.setattr(live_runtime, "_manager", None)
    db = get_db()
    try:
        seed_thread(db, "ui")
        db.execute("UPDATE threads SET user_id=? WHERE id='ui'", (OWNER_USER,))
        created_artifact_id = seed_document(db, "ui")
    finally:
        db.close()
    frames: list[str] = []
    monkeypatch.setattr(
        comment_notifications,
        "broadcast_doc_comment_changed",
        lambda artifact_id, **_kwargs: frames.append(artifact_id),
    )

    response = client.post(
        f"/api/artifacts/{created_artifact_id}/comments?token={OWNER_TOKEN}",
        json={"delivery_policy": "immediate", "body": "Keep canvases current.", "client_id": "no-recipient-comment"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["batch_ids"] == []
    assert frames == [created_artifact_id]


def test_person_comment_is_live_before_edit_idle_delivery(client, isolated_db, monkeypatch):
    from doceditor import comment_notifications, comments, live_runtime

    monkeypatch.setattr(live_runtime, "_manager", None)
    db = get_db()
    try:
        seed_thread(db, "owner")
        db.execute("UPDATE threads SET user_id=? WHERE id='owner'", (OWNER_USER,))
        artifact_id = seed_document(db, "owner")
        root = comments.create(artifact_id, body="Earlier thread", author_kind="user", announce=False, db=db)
        reply = comments.create(artifact_id, body="Unread reply", author="Agent", author_kind="agent",
                                author_thread_id="owner", parent_id=root["id"], announce=False, db=db)
        frames = []
        monkeypatch.setattr(comment_notifications, "broadcast_doc_comment_changed",
                            lambda aid, **kwargs: frames.append((aid, kwargs)))
        url = f"/api/artifacts/{artifact_id}/comments?token={OWNER_TOKEN}"
        posted = client.post(url, json={"body": "Visible now", "client_id": "live-person", "delivery_policy": "immediate"})
        assert posted.status_code == 200, posted.text
        assert len(frames) == 1
        assert frames[0][0] == artifact_id
        assert not frames[0][1]["unread_attention_changed"]
        before = client.get(url).json()
        assert {row["id"] for row in before["comments"]} == {root["id"], "live-person"}
        earlier = next(row for row in before["comments"] if row["id"] == root["id"])
        assert earlier["new_reply_ids"] == [reply["id"]]
        assert before["feedback"]["open_count"] == 1
        [batch] = batches(db, state="open")
        comment_at = datetime.fromisoformat(batch["last_item_at"])
        edit_at = comment_at + timedelta(seconds=1)
        with write_transaction(db, scope="test_edit_after_comment") as connection:
            feedback_batch_edits.record_edit(
                connection, artifact_id=artifact_id, recipient_id="owner", author_kind="user",
                author_id=feedback_batches.USER_AUTHOR_ID, actor_display="Steve",
                path=Path("/workspace/shared/live.md"), before=b"before", after=b"after", at=edit_at.isoformat(),
            )
        [batch] = batches(db, state="open")
        deadline = datetime.fromisoformat(batch["closes_at"])
        assert deadline == edit_at + timedelta(seconds=30)
        assert feedback_batches.close_due((deadline - timedelta(microseconds=1)).isoformat(), db=db) == []
        assert len(frames) == 1
        assert feedback_batches.close_due(deadline.isoformat(), db=db) == ["owner"]
        assert len(frames) == 2
        assert frames[1] == (artifact_id, {})
        after = client.get(url).json()
        assert after["comments"] == before["comments"]
        assert after["feedback"]["open_count"] == 0
    finally:
        db.close()


@pytest.mark.parametrize("path", ["send", "retry"])
def test_feedback_actions_accept_bearer_header_only(client, artifact_id, path):
    response = client.post(
        f"/api/doc-feedback/{path}",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
        json={"artifact_id": artifact_id, **({"delivery_policy": "document-held"} if path == "retry" else {})},
    )

    assert response.status_code == 200, response.text


@pytest.mark.parametrize("path", ["send", "retry"])
def test_feedback_actions_require_bearer_header(client, artifact_id, path):
    response = client.post(f"/api/doc-feedback/{path}", json={"artifact_id": artifact_id})

    assert response.status_code == 401


def test_listing_comments_does_not_change_open_feedback(client, isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        db.execute("UPDATE threads SET user_id=? WHERE id='owner'", (OWNER_USER,))
        artifact_id = seed_document(db, "owner")
        with write_transaction(db, scope="test_open_user_edit") as connection:
            feedback_batch_edits.record_edit(
                connection,
                artifact_id=artifact_id,
                recipient_id="owner",
                author_kind="user",
                author_id=feedback_batches.USER_AUTHOR_ID,
                actor_display="Steve",
                path=Path("/workspace/shared/feedback.md"),
                before=b"before",
                after=b"after",
            )
    finally:
        db.close()

    listed = client.get(f"/api/artifacts/{artifact_id}/comments?token={OWNER_TOKEN}")

    assert listed.status_code == 200, listed.text
    db = get_db()
    try:
        assert batches(db, state="open")[0]["closed_reason"] is None
        assert db.execute("SELECT COUNT(*) FROM doc_comment_thread_views").fetchone()[0] == 0
    finally:
        db.close()
