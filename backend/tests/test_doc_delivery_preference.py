"""The "Deliver immediately" choice follows the person on the server, and a person's saves read it when they land."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auth_fixtures import OTHER_TOKEN, OWNER_TOKEN, OWNER_USER
from doc_feedback_helpers import batches, seed_document, seed_thread
from doceditor import delivery_preference, feedback_batch_edits, feedback_batches
from infra.database import get_db, write_transaction

OWNER = {"Authorization": f"Bearer {OWNER_TOKEN}"}
OTHER = {"Authorization": f"Bearer {OTHER_TOKEN}"}
ROUTE = "/api/doc-feedback/delivery-preference"


@pytest.fixture
def client(isolated_db, monkeypatch):
    from doceditor import live_runtime
    from main import app

    monkeypatch.setattr(live_runtime, "_manager", None)
    return TestClient(app, raise_server_exceptions=False)


def test_an_unset_preference_reads_as_on(client):
    assert client.get(ROUTE, headers=OWNER).json() == {"immediate": True, "stored": False}


def test_the_toggle_writes_it_and_every_read_follows(client):
    assert client.put(ROUTE, headers=OWNER, json={"immediate": False}).json() == {"immediate": False, "stored": True}
    assert client.get(ROUTE, headers=OWNER).json() == {"immediate": False, "stored": True}

    client.put(ROUTE, headers=OWNER, json={"immediate": True})
    assert client.get(ROUTE, headers=OWNER).json() == {"immediate": True, "stored": True}


def test_each_person_has_their_own(client):
    client.put(ROUTE, headers=OWNER, json={"immediate": False})

    assert client.get(ROUTE, headers=OTHER).json() == {"immediate": True, "stored": False}
    client.put(ROUTE, headers=OTHER, json={"immediate": True})
    assert client.get(ROUTE, headers=OWNER).json()["immediate"] is False


def test_it_needs_a_signed_in_person_and_a_boolean(client):
    assert client.get(ROUTE).status_code in (401, 403)
    assert client.put(ROUTE, headers=OWNER, json={"immediate": "maybe"}).status_code == 422


def test_a_change_is_announced_to_that_persons_other_clients(client, monkeypatch):
    sent = []
    from infra import state

    monkeypatch.setattr(state, "broadcast_to_user", lambda user_id, message, **_: sent.append((user_id, message)))
    client.put(ROUTE, headers=OWNER, json={"immediate": False})

    assert sent == [(OWNER_USER, {"type": "doc_delivery_preference_changed", "immediate": False})]


def _save(db, owner_thread: str, *, author_kind: str = "user", author_id: str = feedback_batches.USER_AUTHOR_ID):
    artifact = seed_document(db, owner_thread)
    db.commit()
    with write_transaction(db, scope="test_save_lane") as connection:
        feedback_batch_edits.record_edit(
            connection, artifact_id=artifact, recipient_id=owner_thread, author_kind=author_kind, author_id=author_id,
            actor_display="Steve", path=Path("/workspace/shared/doc.md"), before=b"old\n", after=b"new\n",
        )
    return batches(db)


@pytest.mark.parametrize(
    ("immediate", "lane", "seconds"),
    [(True, feedback_batches.IMMEDIATE, feedback_batches.IMMEDIATE_IDLE), (False, feedback_batches.DOCUMENT_HELD, feedback_batches.USER_IDLE)],
)
def test_a_persons_save_lands_in_the_lane_their_preference_names(isolated_db, immediate, lane, seconds):
    db = get_db()
    try:
        seed_thread(db, "owner")
        delivery_preference.write("owner-token", immediate, db=db)
        [batch] = _save(db, "owner")
        assert batch["delivery_policy"] == lane
        gap = datetime.fromisoformat(batch["closes_at"]) - datetime.fromisoformat(batch["last_item_at"])
        assert gap.total_seconds() == seconds
    finally:
        db.close()


def test_a_save_with_no_choice_made_is_immediate(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        assert [batch["delivery_policy"] for batch in _save(db, "owner")] == [feedback_batches.IMMEDIATE]
    finally:
        db.close()


def test_another_persons_choice_does_not_hold_this_persons_save(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        delivery_preference.write("someone-else", False, db=db)
        assert [batch["delivery_policy"] for batch in _save(db, "owner")] == [feedback_batches.IMMEDIATE]
    finally:
        db.close()


def test_an_agents_save_stays_immediate_whatever_the_person_chose(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "helper")
        delivery_preference.write("owner-token", False, db=db)
        [batch] = _save(db, "owner", author_kind="agent", author_id="helper")
        assert batch["delivery_policy"] == feedback_batches.IMMEDIATE
    finally:
        db.close()


def test_a_save_aimed_at_a_thread_with_no_row_is_immediate(isolated_db):
    db = get_db()
    try:
        assert delivery_preference.lane_for_thread_owner(db, "no-such-thread") == feedback_batches.IMMEDIATE
    finally:
        db.close()
