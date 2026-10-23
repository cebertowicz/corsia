"""HTTP contract for authenticated document-feedback actions."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient

from auth_fixtures import OWNER_TOKEN, OWNER_USER
from doc_feedback_helpers import add_comment, batches, seed_document, seed_thread
from doceditor import feedback_batches
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


def test_user_comment_background_delivery_drives_only_after_the_saved_response(client, isolated_db, monkeypatch):
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
    payload = {"body": "Please change this.", "client_id": "immediate-comment"}
    first = client.post(f"/api/artifacts/{artifact_id}/comments?token={OWNER_TOKEN}", json=payload)
    replay = client.post(f"/api/artifacts/{artifact_id}/comments?token={OWNER_TOKEN}", json=payload)

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert first.json()["woken"] == []
    assert replay.json()["woken"] == []
    db = get_db()
    try:
        [batch] = batches(db)
        assert (batch["state"], batch["closed_reason"], batch["item_count"], batch["attempts"]) == (
            "delivered", "send", 1, 1,
        )
    finally:
        db.close()
    assert drives == [["owner"]]


@pytest.mark.asyncio
async def test_comment_handler_returns_before_a_blocked_background_delivery(isolated_db, monkeypatch):
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

    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_delivery(_batch_ids):
        started.set()
        await release.wait()

    monkeypatch.setattr(doc_comments, "_deliver_person_comment_after_response", blocked_delivery)
    background = BackgroundTasks()
    result = await doc_comments.create_comment(
        created_artifact_id,
        OWNER_TOKEN,
        CommentCreate(body="Save before delivery.", client_id="blocked-delivery"),
        background,
    )

    assert result["comment"]["id"] == "blocked-delivery"
    assert result["woken"] == []
    assert not started.is_set()
    assert len(background.tasks) == 1
    db = get_db()
    try:
        [batch] = batches(db)
        assert (batch["state"], batch["item_count"]) == ("open", 1)
    finally:
        db.close()

    running = asyncio.create_task(background())
    await asyncio.wait_for(started.wait(), timeout=1)
    running.cancel()
    await asyncio.gather(running, return_exceptions=True)


@pytest.mark.parametrize("path", ["send", "retry"])
def test_feedback_actions_accept_bearer_header_only(client, artifact_id, path):
    response = client.post(
        f"/api/doc-feedback/{path}",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
        json={"artifact_id": artifact_id},
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
            feedback_batches.record_edit(
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
