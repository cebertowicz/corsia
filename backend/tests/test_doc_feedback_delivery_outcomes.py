"""What a delivery attempt ended in reaches the person the same way from every trigger."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auth_fixtures import OWNER_TOKEN, OWNER_USER
from doc_feedback_helpers import add_comment, add_person_comment, batches, seed_document, seed_thread
from doceditor import feedback_batch_edits, feedback_batches, feedback_delivery
from infra.database import get_db, write_transaction

HEADERS = {"Authorization": f"Bearer {OWNER_TOKEN}"}
AUTH_PARAMS = {"token": OWNER_TOKEN}
LANE = {"document-held": feedback_batches.DOCUMENT_HELD, "image-held": feedback_batches.IMAGE_HELD}


@pytest.fixture
def client(isolated_db):
    from main import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def drives(monkeypatch):
    from plans import public

    seen: list[list[str]] = []

    async def drive_threads(thread_ids):
        seen.append(list(thread_ids))

    monkeypatch.setattr(public, "drive_threads", drive_threads)
    return seen


@pytest.fixture
def owner_thread(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        db.execute("UPDATE threads SET user_id=? WHERE id='owner'", (OWNER_USER,))
        db.commit()
    finally:
        db.close()
    return "owner"


def _set_model(model: str) -> None:
    db = get_db()
    try:
        db.execute("UPDATE threads SET model=? WHERE id='owner'", (model,))
        db.commit()
    finally:
        db.close()


def _post(client, route: str, **payload):
    response = client.post(f"/api/doc-feedback/{route}", headers=HEADERS, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _feedback(client, artifact_id: str) -> dict:
    response = client.get(f"/api/artifacts/{artifact_id}/comments", params=AUTH_PARAMS)
    assert response.status_code == 200, response.text
    return response.json()["feedback"]


def _failed_immediate_delivery(owner_thread: str) -> str:
    """A person's default-mode comment whose delivery ended undelivered (its owner has no usable model)."""
    db = get_db()
    try:
        artifact = seed_document(db, owner_thread)
        _set_model("missing/model")
        add_person_comment(db, artifact)
    finally:
        db.close()
    return artifact


def test_immediate_failure_is_projected_and_retried_from_the_document_lane(client, owner_thread, drives):
    artifact = _failed_immediate_delivery(owner_thread)

    [failed] = _feedback(client, artifact)["undelivered_batches"]
    assert (failed["delivery_policy"], failed["item_count"]) == ("immediate", 1)

    _set_model("openai/test-checkpoint")
    image = _post(client, "retry", artifact_id=artifact, delivery_policy="image-held")
    assert [batch["delivery_policy"] for batch in image["undelivered_batches"]] == ["immediate"]
    assert image["sent_count"] == 0

    document = _post(client, "retry", artifact_id=artifact, delivery_policy="document-held")
    assert (document["sent_count"], document["undelivered_batches"]) == (1, [])
    assert drives == [[owner_thread]]


def test_a_migrated_undelivered_immediate_row_is_projected(client, owner_thread):
    db = get_db()
    try:
        artifact = seed_document(db, owner_thread)
        add_comment(db, artifact, author_kind="user", author_id=None, author="Steve")
        db.execute("UPDATE doc_feedback_batches SET state='undelivered', undelivered_reason='no_owner'")
        db.commit()
    finally:
        db.close()

    [batch] = _feedback(client, artifact)["undelivered_batches"]

    assert (batch["delivery_policy"], batch["undelivered_reason"]) == ("immediate", "no_owner")


def test_a_failed_send_reports_nothing_sent(client, owner_thread):
    db = get_db()
    try:
        artifact = seed_document(db, owner_thread)
        add_comment(db, artifact, author_kind="user", author_id=None, author="Steve", delivery_policy="document-held")
    finally:
        db.close()
    _set_model("missing/model")

    body = _post(client, "send", artifact_id=artifact, delivery_policy="document-held")

    assert (body["sent_count"], body["undelivered_count"]) == (0, 1)
    assert [batch["delivery_policy"] for batch in body["undelivered_batches"]] == ["document-held"]


def test_a_mixed_send_counts_only_what_was_delivered(client, owner_thread, monkeypatch):
    db = get_db()
    try:
        artifact = seed_document(db, owner_thread)
        for policy, count in (("document-held", 2), ("image-held", 3)):
            for _ in range(count):
                add_comment(db, artifact, author_kind="user", author_id=None, author="Steve", delivery_policy=policy)
    finally:
        db.close()
    deliver = feedback_delivery.deliver_claimed

    def refuse_images(db, batch):
        if batch["delivery_policy"] == "image-held":
            return feedback_delivery.DeliveryResult(reason="owner_unavailable")
        return deliver(db, batch)

    monkeypatch.setattr(feedback_delivery, "deliver_claimed", refuse_images)

    body = _post(client, "send", artifact_id=artifact)

    assert (body["sent_count"], body["undelivered_count"]) == (2, 3)
    assert [batch["delivery_policy"] for batch in body["undelivered_batches"]] == ["image-held"]


def test_thread_send_reports_delivered_and_undelivered_counts(client, owner_thread, monkeypatch):
    db = get_db()
    try:
        delivered = seed_document(db, owner_thread, name="delivered.md")
        refused = seed_document(db, owner_thread, name="refused.md")
        for artifact, count in ((delivered, 2), (refused, 3)):
            for _ in range(count):
                add_comment(db, artifact, author_kind="user", author_id=None, author="Steve", delivery_policy="document-held")
    finally:
        db.close()
    deliver = feedback_delivery.deliver_claimed

    def refuse_one(db, batch):
        if batch["artifact_id"] == refused:
            return feedback_delivery.DeliveryResult(reason="owner_unavailable")
        return deliver(db, batch)

    monkeypatch.setattr(feedback_delivery, "deliver_claimed", refuse_one)

    body = _post(client, "thread/send", thread_id=owner_thread)

    assert (body["sent_count"], body["undelivered_count"]) == (2, 3)
    assert body["undelivered_documents"] == [{"artifact_id": refused, "title": "refused.md"}]


def test_a_retry_that_delivers_reports_what_it_delivered(client, owner_thread, drives):
    db = get_db()
    try:
        artifact = seed_document(db, owner_thread)
        add_comment(db, artifact, author_kind="user", author_id=None, author="Steve", delivery_policy="image-held")
    finally:
        db.close()
    _set_model("missing/model")
    _post(client, "send", artifact_id=artifact, delivery_policy="image-held")
    _set_model("openai/test-checkpoint")

    body = _post(client, "retry", artifact_id=artifact, delivery_policy="image-held")

    assert (body["sent_count"], body["undelivered_batches"]) == (1, [])


def test_an_edit_only_batch_is_open_but_never_held_so_no_send_is_offered(client, owner_thread):
    db = get_db()
    try:
        artifact = seed_document(db, owner_thread)
        with write_transaction(db, scope="test_edit") as connection:
            feedback_batch_edits.record_edit(
                connection, artifact_id=artifact, recipient_id=owner_thread, author_kind="user",
                author_id=feedback_batches.USER_AUTHOR_ID, actor_display="The user",
                path=Path("/workspace/shared/doc.md"), before=b"one\n", after=b"two\n",
            )
    finally:
        db.close()

    feedback = _feedback(client, artifact)
    held = client.get(f"/api/doc-feedback/thread/{owner_thread}", headers=HEADERS).json()
    # A person's save is immediate: open until its 5 s deadline, delivered by the server, never held for a Send.
    assert (feedback["open_count"], feedback["document_held_count"], feedback["image_held_count"]) == (1, 0, 0)
    assert held["artifacts"] == []


def test_a_reopen_follows_the_persons_delivery_policy(client, owner_thread):
    db = get_db()
    try:
        artifact = seed_document(db, owner_thread)
        rows = []
        for _ in range(2):
            row, _feedback_result = add_comment(db, artifact, author_kind="user", author_id=None, author="Steve")
            db.execute("UPDATE doc_comments SET resolved=1 WHERE id=?", (row["id"],))
            rows.append(row["id"])
        db.execute("DELETE FROM doc_feedback_batches")
        db.commit()
    finally:
        db.close()

    for comment_id, policy in zip(rows, ("immediate", "document-held")):
        response = client.patch(
            f"/api/comments/{comment_id}", params=AUTH_PARAMS, json={"resolved": False, "delivery_policy": policy},
        )
        assert response.status_code == 200, response.text

    db = get_db()
    try:
        lanes = {batch["delivery_policy"]: batch for batch in batches(db, state="open")}
    finally:
        db.close()
    assert set(lanes) == {"immediate", "document-held"}
    assert lanes["immediate"]["closes_at"] < lanes["document-held"]["closes_at"]
