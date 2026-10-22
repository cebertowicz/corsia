"""HTTP contract for authenticated document-feedback actions."""

from __future__ import annotations

from pathlib import Path

import pytest
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


def test_user_comment_delivers_once_and_drives_only_after_create_commits(client, isolated_db, monkeypatch):
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
    assert first.json()["woken"] == ["owner"]
    assert replay.json()["woken"] == []
    assert drives == [["owner"]]


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
