"""Authenticated comment tag requests require a viewing scope."""

from auth_fixtures import OWNER_TOKEN, OWNER_USER
from infra.database import get_db
from test_doc_comments_api import artifact_id, doc_file  # noqa: F401


def test_tagged_request_requires_owned_viewing_thread(integration_client, artifact_id):
    url = f"/api/artifacts/{artifact_id}/comments?token={OWNER_TOKEN}"
    request = {"body": "Comment", "delivery_policy": "document-held", "tagged_agent_ids": ["unknown"]}
    for thread_id in (None, "foreign-or-missing"):
        response = integration_client.post(url, json={**request, "thread_id": thread_id})
        assert response.status_code == 400
        assert response.json()["detail"] == "A valid viewing thread_id is required to tag agents"
    response = integration_client.post(url, json={"body": "Comment", "delivery_policy": "document-held"})
    assert response.status_code == 200
    assert response.json()["comment"]["tagged_agents"] == []


def test_tag_create_list_and_idempotent_retry(integration_client, artifact_id):
    db = get_db()
    try:
        for identifier, title, enabled in (("viewer", "Viewer", 0), ("agent", "Named agent", 1)):
            db.execute(
                """INSERT INTO threads(id,user_id,title,created_at,updated_at,agent_enabled,agent_responsibility)
                   VALUES (?,?,?,?,?,?,?)""",
                (identifier, OWNER_USER, title, "2026-01-01", "2026-01-01", enabled, "Review" if enabled else None),
            )
        db.commit()
    finally:
        db.close()
    url = f"/api/artifacts/{artifact_id}/comments?token={OWNER_TOKEN}"
    request = {"body": "Hello", "delivery_policy": "document-held", "thread_id": "viewer",
               "tagged_agent_ids": ["agent"], "client_id": "tag-retry"}
    first = integration_client.post(url, json=request)
    assert first.status_code == 200
    assert first.json()["comment"]["tagged_agents"] == [{"id": "agent", "name": "Named agent"}]
    again = integration_client.post(url, json=request)
    assert again.status_code == 200
    assert again.json()["comment"]["tagged_agents"] == first.json()["comment"]["tagged_agents"]
    listed = integration_client.get(url).json()["comments"]
    assert listed[0]["tagged_agents"] == first.json()["comment"]["tagged_agents"]
