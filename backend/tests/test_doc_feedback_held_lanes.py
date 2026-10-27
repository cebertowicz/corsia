"""Held document and image lanes: lane-scoped sends, the per-document projection, and the thread aggregate."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from auth_fixtures import OWNER_TOKEN, OWNER_USER
from doc_feedback_helpers import add_comment, batches, seed_document, seed_thread
from doceditor import feedback_batches
from infra.database import get_db

HEADERS = {"Authorization": f"Bearer {OWNER_TOKEN}"}


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


def _held(db, artifact_id: str, policy: str, count: int = 1) -> None:
    for _ in range(count):
        add_comment(db, artifact_id, author_kind="user", author_id=None, author="Steve", delivery_policy=policy)


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


def _document_counts(client, artifact_id: str) -> dict:
    response = client.get(f"/api/artifacts/{artifact_id}/comments?token={OWNER_TOKEN}")
    assert response.status_code == 200, response.text
    return response.json()["feedback"]


def test_document_projection_counts_each_held_lane_apart(client, owner_thread):
    db = get_db()
    try:
        artifact = seed_document(db, owner_thread)
        _held(db, artifact, feedback_batches.DOCUMENT_HELD, 2)
        _held(db, artifact, feedback_batches.IMAGE_HELD, 3)
    finally:
        db.close()

    feedback = _document_counts(client, artifact)

    assert (feedback["open_count"], feedback["document_held_count"], feedback["image_held_count"]) == (5, 2, 3)


def test_image_send_closes_only_the_image_lane_and_reports_what_remains(client, owner_thread, drives):
    db = get_db()
    try:
        artifact = seed_document(db, owner_thread)
        _held(db, artifact, feedback_batches.DOCUMENT_HELD, 2)
        _held(db, artifact, feedback_batches.IMAGE_HELD, 3)
    finally:
        db.close()

    response = client.post(
        "/api/doc-feedback/send", headers=HEADERS, json={"artifact_id": artifact, "delivery_policy": "image-held"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["sent_count"], body["open_count"], body["document_held_count"], body["image_held_count"]) == (3, 2, 2, 0)
    db = get_db()
    try:
        states = {batch["delivery_policy"]: batch["state"] for batch in batches(db)}
    finally:
        db.close()
    assert states == {"document-held": "open", "image-held": "delivered"}
    assert drives == [["owner"]]


def test_retry_is_scoped_to_its_held_lane_and_keeps_the_other_failure(client, owner_thread, drives):
    db = get_db()
    try:
        artifact = seed_document(db, owner_thread)
        _held(db, artifact, feedback_batches.DOCUMENT_HELD)
        _held(db, artifact, feedback_batches.IMAGE_HELD)
        db.execute("UPDATE threads SET model='missing/model' WHERE id=?", (owner_thread,))
        db.commit()
    finally:
        db.close()

    for policy in ("document-held", "image-held"):
        response = client.post(
            "/api/doc-feedback/send", headers=HEADERS,
            json={"artifact_id": artifact, "delivery_policy": policy},
        )
        assert response.status_code == 200, response.text

    projected = _document_counts(client, artifact)["undelivered_batches"]
    assert {batch["delivery_policy"] for batch in projected} == {"document-held", "image-held"}

    db = get_db()
    try:
        db.execute("UPDATE threads SET model='openai/test-checkpoint' WHERE id=?", (owner_thread,))
        db.commit()
    finally:
        db.close()

    response = client.post(
        "/api/doc-feedback/retry", headers=HEADERS,
        json={"artifact_id": artifact, "delivery_policy": "document-held"},
    )

    assert response.status_code == 200, response.text
    assert {batch["delivery_policy"] for batch in response.json()["undelivered_batches"]} == {"image-held"}
    db = get_db()
    try:
        states = {batch["delivery_policy"]: batch["state"] for batch in batches(db)}
    finally:
        db.close()
    assert states == {"document-held": "delivered", "image-held": "undelivered"}
    assert drives == [[owner_thread]]


def test_retry_requires_an_owning_lane(client, owner_thread):
    db = get_db()
    try:
        artifact = seed_document(db, owner_thread)
        _held(db, artifact, feedback_batches.DOCUMENT_HELD)
        db.execute("UPDATE threads SET model='missing/model' WHERE id=?", (owner_thread,))
        db.commit()
    finally:
        db.close()
    response = client.post(
        "/api/doc-feedback/send", headers=HEADERS,
        json={"artifact_id": artifact, "delivery_policy": "document-held"},
    )
    assert response.status_code == 200, response.text

    response = client.post("/api/doc-feedback/retry", headers=HEADERS, json={"artifact_id": artifact})

    assert response.status_code == 422
    db = get_db()
    try:
        assert batches(db)[0]["state"] == "undelivered"
    finally:
        db.close()


def test_document_send_leaves_the_image_lane_for_its_viewer(client, owner_thread, drives):
    db = get_db()
    try:
        artifact = seed_document(db, owner_thread)
        _held(db, artifact, feedback_batches.DOCUMENT_HELD, 1)
        _held(db, artifact, feedback_batches.IMAGE_HELD, 2)
    finally:
        db.close()

    body = client.post(
        "/api/doc-feedback/send", headers=HEADERS, json={"artifact_id": artifact, "delivery_policy": "document-held"},
    ).json()

    assert (body["sent_count"], body["document_held_count"], body["image_held_count"]) == (1, 0, 2)


def test_overlapping_lane_sends_deliver_once_and_the_loser_reports_nothing_sent(client, owner_thread, drives):
    db = get_db()
    try:
        artifact = seed_document(db, owner_thread)
        _held(db, artifact, feedback_batches.IMAGE_HELD, 3)
    finally:
        db.close()
    payload = {"artifact_id": artifact, "delivery_policy": "image-held"}

    first = client.post("/api/doc-feedback/send", headers=HEADERS, json=payload).json()
    second = client.post("/api/doc-feedback/send", headers=HEADERS, json=payload).json()

    assert (first["sent_count"], second["sent_count"], second["image_held_count"]) == (3, 0, 0)
    assert second["woken"] == []
    assert drives == [["owner"]]
    db = get_db()
    try:
        [batch] = batches(db)
        assert batch["attempts"] == 1
    finally:
        db.close()


def test_thread_projection_is_document_held_only_with_a_row_per_artifact(client, owner_thread):
    db = get_db()
    try:
        first = seed_document(db, owner_thread, name="first.md")
        second = seed_document(db, owner_thread, name="second.md")
        _held(db, first, feedback_batches.DOCUMENT_HELD, 2)
        _held(db, second, feedback_batches.DOCUMENT_HELD, 2)
        _held(db, second, feedback_batches.IMAGE_HELD, 4)
    finally:
        db.close()

    response = client.get(f"/api/doc-feedback/thread/{owner_thread}", headers=HEADERS)

    assert response.status_code == 200, response.text
    body = response.json()
    # Equal counts from different artifacts stay two rows: the client scopes by artifact, never by count.
    assert body["artifacts"] == [
        {"artifact_id": first, "open_count": 2},
        {"artifact_id": second, "open_count": 2},
    ]
    assert body["open_count"] == 4
    # A read delivers nothing, so it carries no send outcome.
    assert not {"woken", "sent_count", "undelivered_count"} & body.keys()


def test_thread_projection_reads_without_a_write_transaction(client, owner_thread, monkeypatch):
    db = get_db()
    try:
        artifact = seed_document(db, owner_thread)
        _held(db, artifact, feedback_batches.DOCUMENT_HELD)
    finally:
        db.close()

    import api.doc_feedback_routes as routes

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("thread feedback GET must not open a write transaction")

    monkeypatch.setattr(routes, "write_transaction", fail_if_called)

    response = client.get(f"/api/doc-feedback/thread/{owner_thread}", headers=HEADERS)

    assert response.status_code == 200, response.text
    assert response.json()["artifacts"] == [{"artifact_id": artifact, "open_count": 1}]


def test_thread_projection_counts_101_comments_on_one_document(client, owner_thread):
    db = get_db()
    try:
        artifact = seed_document(db, owner_thread)
        _held(db, artifact, feedback_batches.DOCUMENT_HELD, 101)
    finally:
        db.close()

    body = client.get(f"/api/doc-feedback/thread/{owner_thread}", headers=HEADERS).json()

    assert body["open_count"] == 101
    assert body["artifacts"] == [{"artifact_id": artifact, "open_count": 101}]


def test_thread_send_delivers_only_document_held_and_leaves_image_held(client, owner_thread, drives):
    db = get_db()
    try:
        artifact = seed_document(db, owner_thread)
        _held(db, artifact, feedback_batches.DOCUMENT_HELD, 2)
        _held(db, artifact, feedback_batches.IMAGE_HELD, 3)
    finally:
        db.close()

    response = client.post("/api/doc-feedback/thread/send", headers=HEADERS, json={"thread_id": owner_thread})

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["sent_count"], body["open_count"], body["artifacts"]) == (2, 0, [])
    db = get_db()
    try:
        states = {batch["delivery_policy"]: batch["state"] for batch in batches(db)}
    finally:
        db.close()
    assert states == {"document-held": "delivered", "image-held": "open"}
    assert _document_counts(client, artifact)["image_held_count"] == 3


def test_document_and_thread_senders_nest_without_a_second_delivery(client, owner_thread, drives):
    db = get_db()
    try:
        artifact = seed_document(db, owner_thread)
        _held(db, artifact, feedback_batches.DOCUMENT_HELD, 2)
    finally:
        db.close()

    document = client.post(
        "/api/doc-feedback/send", headers=HEADERS, json={"artifact_id": artifact, "delivery_policy": "document-held"},
    ).json()
    thread = client.post("/api/doc-feedback/thread/send", headers=HEADERS, json={"thread_id": owner_thread}).json()
    again = client.post(
        "/api/doc-feedback/send", headers=HEADERS, json={"artifact_id": artifact, "delivery_policy": "document-held"},
    ).json()

    assert (document["sent_count"], thread["sent_count"], again["sent_count"]) == (2, 0, 0)
    assert thread["woken"] == []
    assert drives == [["owner"]]


def test_thread_send_is_refused_for_a_thread_the_user_does_not_own(client, owner_thread):
    db = get_db()
    try:
        seed_thread(db, "someone-else")
        db.commit()
    finally:
        db.close()

    response = client.get("/api/doc-feedback/thread/someone-else", headers=HEADERS)

    assert response.status_code == 404
