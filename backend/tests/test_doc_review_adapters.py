"""N8a real tool and HTTP adapters share identity and scope validation."""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from test_doc_review_requests import review_env  # noqa: F401


@pytest.fixture
def client(review_env, monkeypatch):
    from api import doc_reviews
    from infra.auth import require_user, require_user_header_only
    from plans import public
    async def quiet_drive(threads):
        pass
    monkeypatch.setattr(public, "drive_threads", quiet_drive)
    app = FastAPI()
    app.include_router(doc_reviews.router)
    user = {"id": "owner-token", "name": "Steve"}
    app.dependency_overrides[require_user] = lambda: user
    app.dependency_overrides[require_user_header_only] = lambda: user
    with TestClient(app) as http:
        yield http


def payload(aid, **changes):
    return {"artifact_id": aid, "reviewer": "reviewer", "client_id": "adapter-key", **changes}


def test_user_http_create_list_cancel_and_missing_foreign_thread_have_no_effect(client, review_env):
    db, aid = review_env
    response = client.post("/api/doc-reviews", json=payload(aid))
    assert response.status_code == 422
    response = client.post("/api/doc-reviews", json=payload(aid, thread_id="foreign"))
    assert response.status_code == 404
    assert db.execute("SELECT COUNT(*) FROM doc_review_requests").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 0
    response = client.post("/api/doc-reviews", json=payload(aid, thread_id="requester"))
    assert response.status_code == 200, response.text
    row = response.json()["request"]
    assert row["requester_kind"] == "user"
    assert client.get(f"/api/artifacts/{aid}/reviews").json()["reviews"][0]["id"] == row["id"]
    assert client.delete(f"/api/doc-reviews/{row['id']}").json()["request"]["state"] == "withdrawn"
    assert client.get(f"/api/artifacts/{aid}/reviews").json()["reviews"] == []


def test_cli_http_requires_existing_authoring_seam_and_retries_idempotently(client, review_env):
    from tools.document_reviews import SESSION_REQUIRED, SESSION_REFUSAL
    db, aid = review_env
    missing = client.post("/api/delegation/docs/reviews", json=payload(aid))
    assert missing.status_code == 400 and missing.json()["detail"] == SESSION_REQUIRED
    foreign = client.post("/api/delegation/docs/reviews", json=payload(aid, as_session_id="foreign"))
    assert foreign.status_code == 403 and foreign.json()["detail"] == SESSION_REFUSAL
    first = client.post("/api/delegation/docs/reviews", json=payload(aid, as_session_id="requester"))
    again = client.post("/api/delegation/docs/reviews", json=payload(aid, as_session_id="requester"))
    assert first.status_code == 200, first.text
    assert first.json()["request"]["id"] == again.json()["request"]["id"]
    row = first.json()["request"]
    cancelled = client.delete(f"/api/delegation/docs/reviews/{row['id']}", params={"as_session_id": "requester"})
    assert cancelled.status_code == 200 and cancelled.json()["request"]["state"] == "withdrawn"


@pytest.mark.parametrize("fields", [{}, {"doc_path": "/workspace/shared/PRD.md", "artifact_id": "both"}, {"artifact_id": "foreign"}])
@pytest.mark.asyncio
async def test_tool_xor_and_access_denial_never_write(review_env, fields):
    from tools.document_reviews import DocReviewRequestTool
    db, aid = review_env
    result = await DocReviewRequestTool().execute(**fields, reviewer="reviewer", client_id="tool-key", _thread_id="requester")
    assert result.startswith("Error:"), result
    assert db.execute("SELECT COUNT(*) FROM doc_review_requests").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_http_create_masks_explicit_roster_error(review_env, monkeypatch):
    from api import doc_reviews
    from doceditor import review_requests as reviews
    _db, aid = review_env

    async def broken(*args, **kwargs):
        raise reviews.ReviewRosterError("private schema detail")

    monkeypatch.setattr(doc_reviews, "run_in_db", broken)
    body = doc_reviews.ReviewCreate(artifact_id=aid, reviewer="reviewer", client_id="tool-key")
    actor = reviews.ReviewActor("agent", "requester", "requester", "owner-token", "Requester")
    with pytest.raises(HTTPException) as caught:
        await doc_reviews._create(body, actor)
    assert caught.value.status_code == 503
    assert caught.value.detail == reviews.ROSTER_ERROR


@pytest.mark.asyncio
async def test_tool_roster_failure_returns_safe_copy(review_env, monkeypatch):
    from doceditor import review_requests as reviews
    from tools.document_reviews import DocReviewRequestTool
    _db, aid = review_env

    def broken(*args):
        raise sqlite3.OperationalError("private schema detail")

    monkeypatch.setattr(reviews, "agent_rows", broken)
    result = await DocReviewRequestTool().execute(
        artifact_id=aid, reviewer="reviewer", client_id="tool-key", _thread_id="requester")
    assert result == f"Error: {reviews.ROSTER_ERROR}"
    assert "private schema detail" not in result


@pytest.mark.asyncio
async def test_tool_masks_explicit_roster_error(review_env, monkeypatch):
    from doceditor import review_requests as reviews
    from tools import document_reviews
    _db, aid = review_env

    async def broken(*args, **kwargs):
        raise reviews.ReviewRosterError("private schema detail")

    monkeypatch.setattr(document_reviews, "run_in_db", broken)
    result = await document_reviews.DocReviewRequestTool().execute(
        artifact_id=aid, reviewer="reviewer", client_id="tool-key", _thread_id="requester")
    assert result == f"Error: {reviews.ROSTER_ERROR}"
    assert "private schema detail" not in result


@pytest.mark.asyncio
async def test_tool_uses_real_context_schema_and_approved_description(review_env, monkeypatch):
    from tools.document_reviews import DocReviewRequestTool, TOOL_DESCRIPTION
    from plans import public
    db, aid = review_env
    async def quiet_drive(threads):
        pass
    monkeypatch.setattr(public, "drive_threads", quiet_drive)
    tool = DocReviewRequestTool()
    assert tool.description == TOOL_DESCRIPTION
    assert set(tool.parameters["required"]) == {"reviewer", "client_id"}
    result = json.loads(await tool.execute(artifact_id=aid, reviewer="reviewer", client_id="tool-key", _thread_id="requester"))
    assert result["request"]["requester_id"] == "requester"
    assert result["request"]["reviewer_thread_id"] == "reviewer"
    assert result["request"]["state"] == "forked"
    assert result["request"]["node_id"] is None
    from core.artifact_review_runtime import start
    await start(result["request"]["id"])
    assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_registered_path_resolves_existing_identity_without_auto_registration(review_env, monkeypatch):
    from infra import config
    from registry import models
    from tools.document_reviews import DocReviewRequestTool
    from plans import public
    db, _aid = review_env
    path = str(config.WORKSPACE / "shared" / "path-review.md")
    artifact = models.register_artifact(integration="workspace", type="document", external_id=path,
        parent_thread_id="owner", title="Path review", path=path, db=db)
    db.commit()
    async def quiet_drive(threads):
        pass
    monkeypatch.setattr(public, "drive_threads", quiet_drive)
    tool = DocReviewRequestTool(workspace=config.WORKSPACE)
    text = await tool.execute(doc_path=path, reviewer="reviewer", client_id="path-key", _thread_id="requester")
    assert not text.startswith("Error:"), text
    result = json.loads(text)
    assert result["request"]["artifact_id"] == artifact["id"]
    count = db.execute("SELECT COUNT(*) FROM artifact_identities").fetchone()[0]
    refused = await tool.execute(doc_path=str(config.WORKSPACE / "shared" / "unregistered.md"),
        reviewer="reviewer", client_id="missing-path-key", _thread_id="requester")
    assert "register it first" in refused
    assert db.execute("SELECT COUNT(*) FROM artifact_identities").fetchone()[0] == count
    assert db.execute("SELECT COUNT(*) FROM doc_review_requests").fetchone()[0] == 1


@pytest.mark.parametrize("key", ["", "bad key", "a" * 65])
def test_http_bad_client_ids_use_shared_validator(client, review_env, key):
    from doceditor.comments import validate_client_id
    db, aid = review_env
    with pytest.raises(ValueError) as expected:
        validate_client_id(key)
    response = client.post("/api/doc-reviews", json=payload(aid, thread_id="requester", client_id=key))
    assert response.status_code == 400 and response.json()["detail"] == str(expected.value)
    assert db.execute("SELECT COUNT(*) FROM doc_review_requests").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_tool_foreign_existing_artifact_is_denied(review_env):
    from tools.document_reviews import DocReviewRequestTool
    from doceditor import review_requests as reviews
    db, aid = review_env
    db.execute("UPDATE threads SET user_id='another-user' WHERE id='owner'")
    db.commit()
    result = await DocReviewRequestTool().execute(artifact_id=aid, reviewer="reviewer", client_id="tool-key", _thread_id="requester")
    assert result == f"Error: {reviews.ARTIFACT_REFUSAL}"
    assert db.execute("SELECT COUNT(*) FROM doc_review_requests").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 0


def test_user_mutation_requires_authenticated_header_and_missing_key_is_refused(review_env, client):
    from api.doc_reviews import router
    db, aid = review_env
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as http:
        response = http.post("/api/doc-reviews", json=payload(aid, thread_id="requester"))
    assert response.status_code == 401
    missing_key = payload(aid, thread_id="requester")
    del missing_key["client_id"]
    assert client.post("/api/doc-reviews", json=missing_key).status_code == 422
    assert db.execute("SELECT COUNT(*) FROM doc_review_requests").fetchone()[0] == 0
