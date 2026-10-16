"""Per-thread reply-view stamps preserve the passive reply marker semantics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from auth_fixtures import OTHER_TOKEN, OWNER_TOKEN, OWNER_USER
from doc_feedback_helpers import add_comment, seed_document, seed_thread
from infra.database import get_db, write_transaction


@pytest.fixture
def client(isolated_db):
    from main import app

    return TestClient(app, raise_server_exceptions=False)


def _thread_with_replies(db):
    seed_thread(db, "owner")
    db.execute("UPDATE threads SET user_id=? WHERE id='owner'", (OWNER_USER,))
    artifact_id = seed_document(db, "owner")
    root, _ = add_comment(db, artifact_id, author_kind="user", author="Steve")
    first, _ = add_comment(db, artifact_id, author_id="owner", author="Owner", parent_id=root["id"])
    second, _ = add_comment(db, artifact_id, author_id="owner", author="Owner", parent_id=root["id"])
    older = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    db.execute("UPDATE doc_comments SET created_at=? WHERE id=?", (older, first["id"]))
    return artifact_id, root, first, second


def test_viewed_route_rejects_foreign_root_and_nonreply(client, isolated_db):
    db = get_db()
    try:
        artifact_id, root, _first, _second = _thread_with_replies(db)
        other = seed_document(db, "owner")
        other_root, _ = add_comment(db, other, author_kind="user", author="Steve")
    finally:
        db.close()

    headers = {"Authorization": f"Bearer {OWNER_TOKEN}"}
    wrong_document = client.post(
        f"/api/artifacts/{artifact_id}/comments/{other_root['id']}/viewed",
        headers=headers, json={"seen_reply_id": root["id"]},
    )
    not_reply = client.post(
        f"/api/artifacts/{artifact_id}/comments/{root['id']}/viewed",
        headers=headers, json={"seen_reply_id": root["id"]},
    )
    assert (wrong_document.status_code, not_reply.status_code) == (404, 404)


def test_viewed_route_hides_another_users_document(client, isolated_db):
    db = get_db()
    try:
        artifact_id, root, _first, second = _thread_with_replies(db)
    finally:
        db.close()

    response = client.post(
        f"/api/artifacts/{artifact_id}/comments/{root['id']}/viewed",
        headers={"Authorization": f"Bearer {OTHER_TOKEN}"}, json={"seen_reply_id": second["id"]},
    )

    db = get_db()
    try:
        stamps = db.execute("SELECT COUNT(*) FROM doc_comment_thread_views").fetchone()[0]
    finally:
        db.close()
    assert (response.status_code, stamps) == (404, 0)


@pytest.mark.asyncio
async def test_thread_view_nudge_from_a_database_worker_reaches_the_event_loop(isolated_db, monkeypatch):
    import asyncio
    import threading

    import infra.app_loop as app_loop
    from doceditor import comment_notifications, feedback_state
    from infra.db_async import run_in_db
    from infra.state import ws as sockets

    db = get_db()
    try:
        artifact_id, root, first, _second = _thread_with_replies(db)
    finally:
        db.close()

    arrived = asyncio.Event()
    sent: list[tuple[dict, int]] = []

    class Socket:
        async def send_json(self, message: dict) -> None:
            sent.append((message, threading.get_ident()))
            arrived.set()

    monkeypatch.setattr(app_loop, "_LOOP", asyncio.get_running_loop())
    monkeypatch.setattr(sockets, "_ws_thread_clients", {})
    monkeypatch.setattr(sockets, "_ws_user_clients", {OWNER_USER: [Socket()]})

    def write() -> int:
        connection = get_db()
        try:
            with write_transaction(connection, scope="test_thread_view_nudge") as transaction:
                feedback_state.mark_thread_viewed(
                    transaction, OWNER_USER, artifact_id, root["id"], first["id"],
                )
                comment_notifications.announce_after_commit(transaction, artifact_id)
            return threading.get_ident()
        finally:
            connection.close()

    worker = await run_in_db(write)
    await asyncio.wait_for(arrived.wait(), timeout=5)
    assert worker != threading.get_ident()
    assert sent == [
        ({"type": "doc_comment_changed", "artifact_id": artifact_id}, threading.get_ident()),
    ]


def test_viewed_route_is_monotonic_and_leaves_a_racing_reply_waiting(client, isolated_db):
    db = get_db()
    try:
        artifact_id, root, first, second = _thread_with_replies(db)
    finally:
        db.close()

    headers = {"Authorization": f"Bearer {OWNER_TOKEN}"}
    newest = client.post(
        f"/api/artifacts/{artifact_id}/comments/{root['id']}/viewed",
        headers=headers, json={"seen_reply_id": second["id"]},
    )
    older = client.post(
        f"/api/artifacts/{artifact_id}/comments/{root['id']}/viewed",
        headers=headers, json={"seen_reply_id": first["id"]},
    )
    assert newest.json() == {"root_id": root["id"], "reply_waiting": False}
    assert older.json() == {"root_id": root["id"], "reply_waiting": False}

    db = get_db()
    try:
        seen = db.execute(
            "SELECT seen_reply_id FROM doc_comment_thread_views WHERE user_id=? AND root_id=?",
            (OWNER_USER, root["id"]),
        ).fetchone()
        assert seen["seen_reply_id"] == second["id"]
        third, _ = add_comment(
            db, artifact_id, author_id="owner", author="Owner", parent_id=root["id"],
        )
    finally:
        db.close()

    raced = client.post(
        f"/api/artifacts/{artifact_id}/comments/{root['id']}/viewed",
        headers=headers, json={"seen_reply_id": second["id"]},
    )
    assert raced.json() == {"root_id": root["id"], "reply_waiting": True}
    assert third["id"]


def _listed(client, artifact_id: str) -> dict[str, dict]:
    listed = client.get(f"/api/artifacts/{artifact_id}/comments", params={"token": OWNER_TOKEN})
    assert listed.status_code == 200
    return {thread["id"]: thread for thread in listed.json()["comments"]}


def test_list_names_the_new_replies_and_clears_them_once_stamped(client, isolated_db):
    db = get_db()
    try:
        artifact_id, root, first, second = _thread_with_replies(db)
    finally:
        db.close()

    before = _listed(client, artifact_id)[root["id"]]
    assert (before["new_reply_ids"], before["reply_waiting"]) == ([first["id"], second["id"]], True)

    stamped = client.post(
        f"/api/artifacts/{artifact_id}/comments/{root['id']}/viewed",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"}, json={"seen_reply_id": first["id"]},
    )
    assert stamped.status_code == 200
    partly = _listed(client, artifact_id)[root["id"]]
    assert (partly["new_reply_ids"], partly["reply_waiting"]) == ([second["id"]], True)

    client.post(
        f"/api/artifacts/{artifact_id}/comments/{root['id']}/viewed",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"}, json={"seen_reply_id": second["id"]},
    )
    after = _listed(client, artifact_id)[root["id"]]
    assert (after["new_reply_ids"], after["reply_waiting"]) == ([], False)


def test_a_users_own_reply_is_never_new_and_a_thread_they_did_not_touch_has_none(client, isolated_db):
    db = get_db()
    try:
        artifact_id, root, first, second = _thread_with_replies(db)
        db.commit()
        own, _ = add_comment(
            db, artifact_id, author_kind="user", author_id=None, author="Steve", parent_id=root["id"],
        )
        agent_root, _ = add_comment(db, artifact_id, author_id="owner", author="Owner")
        agent_reply, _ = add_comment(
            db, artifact_id, author_id="owner", author="Owner", parent_id=agent_root["id"],
        )
    finally:
        db.close()

    threads = _listed(client, artifact_id)
    assert threads[root["id"]]["new_reply_ids"] == [first["id"], second["id"]]
    assert own["id"] not in threads[root["id"]]["new_reply_ids"]
    assert agent_reply["id"] not in threads[agent_root["id"]]["new_reply_ids"]
    assert (threads[agent_root["id"]]["new_reply_ids"], threads[agent_root["id"]]["reply_waiting"]) == ([], False)

    client.post(
        f"/api/artifacts/{artifact_id}/comments/{root['id']}/viewed",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"}, json={"seen_reply_id": second["id"]},
    )
    stamped = _listed(client, artifact_id)[root["id"]]
    assert (stamped["new_reply_ids"], stamped["reply_waiting"]) == ([], False)


def test_new_replies_use_one_query_per_list(client, isolated_db):
    from doceditor import feedback_state

    db = get_db()
    try:
        artifact_id, root, _first, _second = _thread_with_replies(db)
        threads = [{"id": root["id"]}]
        statements: list[str] = []
        db.set_trace_callback(statements.append)
        feedback_state.annotate_thread_attention(db, OWNER_USER, artifact_id, threads)
        db.set_trace_callback(None)
    finally:
        db.close()
    assert len([sql for sql in statements if "doc_comment_thread_views" in sql]) == 1
