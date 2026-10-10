"""HTTP contract for authenticated document-feedback actions."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from auth_fixtures import OWNER_TOKEN, OWNER_USER
from doc_feedback_helpers import add_comment, batches, seed_document, seed_thread
from infra.database import get_db


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


@pytest.mark.parametrize("path", ["send", "leave", "return", "retry"])
def test_feedback_actions_accept_bearer_header_only(client, artifact_id, path):
    response = client.post(
        f"/api/doc-feedback/{path}",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
        json={"artifact_id": artifact_id},
    )

    assert response.status_code == 200, response.text


@pytest.mark.parametrize("path", ["send", "leave", "return", "retry"])
def test_feedback_actions_require_bearer_header(client, artifact_id, path):
    response = client.post(f"/api/doc-feedback/{path}", json={"artifact_id": artifact_id})

    assert response.status_code == 401


def test_listing_comments_is_a_read_and_return_does_not_clear_replies(client, isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        db.execute("UPDATE threads SET user_id=? WHERE id='owner'", (OWNER_USER,))
        artifact_id = seed_document(db, "owner")
        add_comment(db, artifact_id, author_kind="user", author_id=None, author="Steve")
    finally:
        db.close()
    headers = {"Authorization": f"Bearer {OWNER_TOKEN}"}
    assert client.post(
        "/api/doc-feedback/leave", headers=headers, json={"artifact_id": artifact_id},
    ).status_code == 200

    listed = client.get(f"/api/artifacts/{artifact_id}/comments?token={OWNER_TOKEN}")

    assert listed.status_code == 200, listed.text
    db = get_db()
    try:
        assert batches(db, state="open")[0]["closed_reason"] == "leave"
        assert db.execute("SELECT COUNT(*) FROM doc_comment_thread_views").fetchone()[0] == 0
    finally:
        db.close()

    assert client.post(
        "/api/doc-feedback/return", headers=headers, json={"artifact_id": artifact_id},
    ).status_code == 200
    db = get_db()
    try:
        assert batches(db, state="open")[0]["closed_reason"] is None
        assert db.execute("SELECT COUNT(*) FROM doc_comment_thread_views").fetchone()[0] == 0
    finally:
        db.close()
