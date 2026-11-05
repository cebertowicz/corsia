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
        assert response.json() == {"detail": "A valid viewing thread_id is required to tag agents",
                                   "kind": "no_viewing_thread"}
    response = integration_client.post(url, json={"body": "Comment", "delivery_policy": "document-held"})
    assert response.status_code == 200
    assert response.json()["comment"]["tagged_agents"] == []


def _seed_viewer_and_agent():
    db = get_db()
    try:
        for identifier, title, enabled in (("viewer", "Viewer", 0), ("agent", "Named agent", 1)):
            db.execute(
                """INSERT OR IGNORE INTO threads(id,user_id,title,created_at,updated_at,agent_enabled,agent_responsibility)
                   VALUES (?,?,?,?,?,?,?)""",
                (identifier, OWNER_USER, title, "2026-01-01", "2026-01-01", enabled, "Review" if enabled else None),
            )
        db.commit()
    finally:
        db.close()


def test_tag_outside_viewer_roster_is_refused_by_kind(integration_client, artifact_id):
    _seed_viewer_and_agent()
    url = f"/api/artifacts/{artifact_id}/comments?token={OWNER_TOKEN}"
    response = integration_client.post(url, json={
        "body": "@Gone", "delivery_policy": "document-held", "thread_id": "viewer", "tagged_agent_ids": ["gone"],
        "tagged_agent_mentions": [{"agent_id": "gone", "start": 0, "end": 5}],
    })
    assert response.status_code == 400
    assert response.json() == {"detail": "Tagged agents must be in the viewing thread's visible roster",
                               "kind": "tag_unavailable"}


def test_tag_create_list_and_idempotent_retry(integration_client, artifact_id, monkeypatch):
    from doceditor import live_events

    events = []
    monkeypatch.setattr(live_events, "emit", lambda action, **properties: events.append((action, properties)))
    _seed_viewer_and_agent()
    reviews = integration_client.get(f"/api/artifacts/{artifact_id}/reviews?token={OWNER_TOKEN}&thread_id=viewer")
    assert reviews.status_code == 200, reviews.text
    assert reviews.json()["taggable_agents"] == [{"id": "agent", "name": "Named agent", "responsibility": "Review"}]
    url = f"/api/artifacts/{artifact_id}/comments?token={OWNER_TOKEN}"
    mentions = [{"agent_id": "agent", "start": 3, "end": 9}, {"agent_id": "agent", "start": 10, "end": 16}]
    request = {"body": "😀 @Agent @Agent", "delivery_policy": "document-held", "thread_id": "viewer",
               "tagged_agent_ids": ["agent", "agent"], "tagged_agent_mentions": mentions, "client_id": "tag-retry"}
    first = integration_client.post(url, json=request)
    assert first.status_code == 200, first.text
    assert first.json()["comment"]["tagged_agents"] == [{"id": "agent", "name": "Named agent"}]
    assert first.json()["comment"]["tagged_agent_mentions"] == mentions
    again = integration_client.post(url, json=request)
    assert again.status_code == 200
    # One event per new tagged comment, counting distinct agents; the idempotent retry adds none.
    assert [event for event in events if event[0] == "comment_tagged"] == [
        ("comment_tagged", {"tag_count": 1, "reply": False, "author_kind": "user"})]
    assert again.json()["comment"]["tagged_agents"] == first.json()["comment"]["tagged_agents"]
    listed = integration_client.get(url).json()["comments"]
    assert listed[0]["tagged_agents"] == first.json()["comment"]["tagged_agents"]
    assert listed[0]["tagged_agent_mentions"] == mentions
    # Both transports accept offsets in the exact submitted text, then retain
    # positions relative to the stripped text they actually store.
    from delegation_docs_fixtures import _post
    submitted = [{"agent_id": "agent", "start": 5, "end": 11}]
    expected = [{"agent_id": "agent", "start": 3, "end": 9}]
    spaced = {"body": "  😀 @Agent  ", "tagged_agent_ids": ["agent"], "tagged_agent_mentions": submitted}
    person = integration_client.post(url, json={**spaced, "thread_id": "viewer", "delivery_policy": "document-held"})
    assert person.status_code == 200, person.text
    assert person.json()["comment"]["body"] == "😀 @Agent"
    assert person.json()["comment"]["tagged_agent_mentions"] == expected
    agent = _post(integration_client, OWNER_TOKEN, **spaced, as_session_id="viewer", parent_id=listed[0]["id"])
    assert agent.status_code == 200, agent.text
    assert agent.json()["comment"]["body"] == "😀 @Agent"
    assert agent.json()["comment"]["tagged_agent_mentions"] == expected
    invalid_spaced = {**spaced, "tagged_agent_mentions": [{"agent_id": "agent", "start": 0, "end": 5}]}
    assert integration_client.post(url, json={**invalid_spaced, "thread_id": "viewer", "delivery_policy": "document-held"}).status_code == 400
    assert _post(integration_client, OWNER_TOKEN, **invalid_spaced, as_session_id="viewer", parent_id=listed[0]["id"]).status_code == 422
    reply = integration_client.post(url, json={**request, "parent_id": listed[0]["id"], "client_id": "tag-reply"})
    assert reply.status_code == 200
    assert reply.json()["comment"]["tagged_agent_mentions"] == mentions
    assert events[-1] == ("comment_tagged", {"tag_count": 1, "reply": True, "author_kind": "user"})
    for invalid in (
        {"agent_id": "other", "start": 3, "end": 9},
        {"agent_id": "agent", "start": 1, "end": 9},
        {"agent_id": "agent", "start": 3, "end": 99},
        {"agent_id": "agent", "start": 9, "end": 3},
    ):
        response = integration_client.post(url, json={**request, "client_id": None, "tagged_agent_mentions": [invalid]})
        assert response.status_code == 400
