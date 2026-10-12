"""The review bar over HTTP: its read, and the clear route's verdict, note and typed refusals."""

from __future__ import annotations

import uuid

import pytest
from auth_fixtures import OWNER_TOKEN, OWNER_USER

from doc_feedback_helpers import add_comment, seed_document

pytestmark = pytest.mark.integration


def _asked_document(thread_id: str) -> str:
    from infra.database import get_db
    from plans import service
    from plans.boundary import PlanActor, mutate

    db = get_db()
    try:
        db.execute(
            "INSERT OR IGNORE INTO threads (id,user_id,title,model,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (thread_id, OWNER_USER, "Style", "openai/test-checkpoint", "t", "t"),
        )
        db.commit()
        artifact = seed_document(db, thread_id, name=f"{thread_id}.md")
        plan = service.ensure_plan("thread", thread_id, db=db)
        node = service.add_node(
            plan.id, kind="task", title="Review the proposal",
            acceptance_criteria="Reviewed", db=db,
        )
        db.commit()
        mutate(
            plan.id, "block_node", {
                "node_id": node.id, "reason_key": "review", "reason": "Read the proposal",
                "owner_kind": "user", "owner_id": None, "ask_artifact_id": artifact,
                "expected_revision": service._node_row(db, node.id)["revision"],
            }, actor=PlanActor(thread_id, OWNER_USER, None), provenance="test.review_bar",
            mutation_id=uuid.uuid4().hex, db=db,
        )
        db.commit()
        return artifact
    finally:
        db.close()


def _comment(artifact: str) -> None:
    from infra.database import get_db

    db = get_db()
    try:
        add_comment(db, artifact, author_kind="user", author="Steve", author_id=None)
    finally:
        db.close()


def _clear(client, artifact: str, **body):
    return client.post(
        f"/api/attention/{artifact}/clear",
        json={"token": OWNER_TOKEN, **{"outcome": "answer", **body}},
    )


def _review(client, artifact: str) -> dict:
    response = client.get(
        f"/api/attention/{artifact}/review", headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
    )
    assert response.status_code == 200
    return response.json()


def test_the_read_carries_the_ask_and_the_round_count(integration_client):
    artifact = _asked_document(f"rb-read-{uuid.uuid4().hex[:6]}")

    before = _review(integration_client, artifact)
    _comment(artifact)
    after = _review(integration_client, artifact)

    assert before["ask_raised"] is True and before["ask_reason"] == "Read the proposal"
    assert len(before["hold_ids"]) == 1
    assert before["asker"]["name"] == "Style"
    assert before["round_feedback_count"] == 0
    assert after["round_feedback_count"] == 1


def test_request_changes_without_feedback_or_note_is_refused_with_a_code(integration_client):
    artifact = _asked_document(f"rb-note-{uuid.uuid4().hex[:6]}")

    refused = _clear(integration_client, artifact, verdict="changes_requested")

    assert refused.status_code == 422
    assert refused.json()["detail"]["code"] == "note_required"
    sent = _clear(
        integration_client, artifact, verdict="changes_requested", note="Cut section 3.",
    )
    assert sent.status_code == 200
    assert sent.json()["review"] == {
        "verdict": "changes_requested", "note": "Cut section 3.",
        "feedback_counts": {"comments": 0, "suggestions": 0, "edits": 0},
    }


def test_a_second_answer_is_stale_and_clears_nothing(integration_client):
    artifact = _asked_document(f"rb-stale-{uuid.uuid4().hex[:6]}")
    assert _clear(integration_client, artifact, verdict="approved").status_code == 200

    again = _clear(integration_client, artifact, verdict="approved")

    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "ask_stale"


def test_a_verdict_on_another_outcome_is_a_400(integration_client):
    artifact = _asked_document(f"rb-outcome-{uuid.uuid4().hex[:6]}")

    response = _clear(integration_client, artifact, outcome="dismiss", verdict="approved")

    assert response.status_code == 400


def test_an_answer_drives_the_asker_once_its_write_commits(integration_client, monkeypatch):
    from plans import public

    driven: list[tuple[str, ...]] = []

    async def drive_threads(thread_ids):
        driven.append(tuple(thread_ids))

    monkeypatch.setattr(public, "drive_threads", drive_threads)
    thread = f"rb-drive-{uuid.uuid4().hex[:6]}"
    artifact = _asked_document(thread)

    assert _clear(integration_client, artifact, verdict="approved").status_code == 200

    assert driven == [(thread,)]
