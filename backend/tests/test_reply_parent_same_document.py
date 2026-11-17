"""A reply belongs to the document its parent comment is on, on every route that creates one.

Routes that create a reply: the browser's ``POST /api/artifacts/{id}/comments`` (the document comes
from the URL), the ``corsia doc`` CLI's ``POST /api/delegation/docs/comments`` and the agent
``doc_comment_create`` tool (both take the document from the parent; the tool's refusal of a second
target is ``test_document_artifact_tools``), a queued agent reply replayed later, and a review fork
(``test_review_fork_enforcement``). All of them write through ``comments.create_with_status``.
"""

from __future__ import annotations

import uuid

import pytest
from auth_fixtures import OWNER_TOKEN

pytestmark = pytest.mark.integration

_TEXT = "# Notes\n\nThe router decides where an inbound event goes.\n"
_QUOTE = "router decides"


def _register(client, workspace, name: str) -> str:
    path = workspace / f"{name}-{uuid.uuid4().hex[:8]}.md"
    path.write_text(_TEXT)
    response = client.post("/api/files/register", params={"token": OWNER_TOKEN}, json={"path": str(path)})
    assert response.status_code == 200, response.text
    return response.json()["artifact"]["id"]


@pytest.fixture
def documents(integration_client, integration_workspace):
    return _register(integration_client, integration_workspace, "first"), _register(
        integration_client, integration_workspace, "second")


def _post(client, artifact_id: str, **fields):
    return client.post(f"/api/artifacts/{artifact_id}/comments", params={"token": OWNER_TOKEN},
                       json={"delivery_policy": "immediate", **fields})


def _agreed_thread(client, artifact_id: str) -> dict:
    """A thread that closed because everyone in it agreed: a new reply from the person would re-open it."""
    from doceditor import comments

    start = _TEXT.index(_QUOTE)
    response = _post(client, artifact_id, body="Tighten this.", anchor_text=_QUOTE,
                     from_off=start, to_off=start + len(_QUOTE))
    assert response.status_code == 200, response.text
    root_id = response.json()["comment"]["id"]
    comments.update(root_id, {"resolved": True, "closed_by_agreement": True})
    return comments.get_comment(root_id)


def _replies(client, artifact_id: str) -> list[dict]:
    threads = client.get(f"/api/artifacts/{artifact_id}/comments", params={"token": OWNER_TOKEN}).json()["comments"]
    return [reply for thread in threads for reply in thread["replies"]]


def test_a_reply_must_name_a_parent_on_the_same_document(integration_client, documents):
    from doceditor import comments

    first, second = documents
    root = _agreed_thread(integration_client, first)
    client_id = f"r-{uuid.uuid4().hex[:8]}"

    response = _post(integration_client, second, body="Reply", parent_id=root["id"], client_id=client_id)

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Comment not found"
    assert comments.get_comment(client_id) is None
    assert comments.get_comment(root["id"]) == root  # still closed on agreement, nothing about it changed
    assert _replies(integration_client, first) == [] and _replies(integration_client, second) == []


def test_a_reply_on_the_same_document_is_kept_and_reopens_its_agreed_thread(integration_client, documents):
    from doceditor import comments

    first, _second = documents
    root = _agreed_thread(integration_client, first)

    response = _post(integration_client, first, body="Reply", parent_id=root["id"])

    assert response.status_code == 200, response.text
    reply = response.json()["comment"]
    assert (reply["artifact_id"], reply["parent_id"]) == (first, root["id"])
    assert comments.get_comment(root["id"])["resolved"] == 0


def test_a_reply_to_a_reply_joins_the_root_thread_of_the_same_document(integration_client, documents):
    first, _second = documents
    root = _agreed_thread(integration_client, first)
    child = _post(integration_client, first, body="First reply", parent_id=root["id"]).json()["comment"]

    response = _post(integration_client, first, body="Second reply", parent_id=child["id"])

    assert response.status_code == 200, response.text
    assert response.json()["comment"]["parent_id"] == root["id"]


def test_a_reply_must_name_a_parent_that_exists(integration_client, documents):
    from doceditor import comments

    first, _second = documents
    client_id = f"r-{uuid.uuid4().hex[:8]}"

    response = _post(integration_client, first, body="Reply", parent_id="no-such-comment", client_id=client_id)

    assert response.status_code == 404, response.text
    assert comments.get_comment(client_id) is None


def test_the_cli_route_takes_a_replys_document_from_its_parent(integration_client, documents):
    from doceditor import comments

    first, second = documents
    root = _agreed_thread(integration_client, first)
    thread = integration_client.post("/api/threads", params={"token": OWNER_TOKEN}, json={"title": "worker"})
    assert thread.status_code == 200, thread.text
    client_id = f"r-{uuid.uuid4().hex[:8]}"

    response = integration_client.post(
        "/api/delegation/docs/comments", params={"token": OWNER_TOKEN},
        json={"body": "Reply", "parent_id": root["id"], "artifact_id": second,
              "as_session_id": thread.json()["thread_id"], "client_id": client_id})

    assert response.status_code == 400, response.text
    assert comments.get_comment(client_id) is None
    assert comments.get_comment(root["id"]) == root


def test_storage_refuses_a_reply_whose_parent_is_on_another_document(integration_client, documents):
    """Every create route writes through here, a queued agent reply replayed later included."""
    from doceditor import comments

    first, second = documents
    root = _agreed_thread(integration_client, first)
    client_id = f"r-{uuid.uuid4().hex[:8]}"

    with pytest.raises(comments.ReplyParentNotFound):
        comments.create_with_status(second, body="Reply", parent_id=root["id"], client_id=client_id, announce=False)

    assert comments.get_comment(client_id) is None
    assert comments.get_comment(root["id"]) == root
