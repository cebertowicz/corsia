"""A pane still open on a promoted document's old id never writes to the retired row (#2617).

A promote retires the working file's identity and carries its comment state to the successor. A
pane that had the document open keeps the old id, so its writes either follow the document to the
live successor or are refused with ``NOT_COMMENTABLE``, which leaves the browser holding the draft.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from auth_fixtures import OTHER_USER, OWNER_TOKEN, OWNER_USER
from api.doc_comment_access import NOT_COMMENTABLE
from doc_feedback_helpers import add_comment, seed_document, seed_thread
from infra.database import get_db, write_transaction
from registry import models

TOKEN = {"token": OWNER_TOKEN}
HEADERS = {"Authorization": f"Bearer {OWNER_TOKEN}"}
LANE = "immediate"


@pytest.fixture
def client(isolated_db, monkeypatch):
    from doceditor import live_runtime
    from main import app

    # This client runs no lifespan, so a manager left by an integration test's app has no loop here.
    monkeypatch.setattr(live_runtime, "_manager", None)
    return TestClient(app, raise_server_exceptions=False)


def _document_with_thread(db):
    seed_thread(db, "owner")
    db.execute("UPDATE threads SET user_id=? WHERE id='owner'", (OWNER_USER,))
    artifact = seed_document(db, "owner")
    root, _ = add_comment(db, artifact, author_kind="user", author="Steve")
    return artifact, root


def _promote(db, artifact, *, owner="owner"):
    """Retire ``artifact`` into a new document, which carries its comments, as a promote does."""
    successor = seed_document(db, owner)
    with write_transaction(db, scope="test_promote") as connection:
        models.retire(artifact, tombstone=True, cause="promoted", successor=successor, db=connection)
    return successor


def _reply(client, artifact, root, text="from the open pane"):
    return client.post(
        f"/api/artifacts/{artifact}/comments", params=TOKEN,
        json={"body": text, "parent_id": root["id"], "delivery_policy": LANE},
    )


def _rows(db, artifact):
    return [row["body"] for row in db.execute("SELECT body FROM doc_comments WHERE artifact_id=? ORDER BY created_at, id", (artifact,))]


def test_a_reply_from_a_pane_on_the_old_id_lands_on_the_successor(client, isolated_db):
    db = get_db()
    try:
        artifact, root = _document_with_thread(db)
        successor = _promote(db, artifact)
        response = _reply(client, artifact, root)
        assert response.status_code == 200, response.text
        assert response.json()["comment"]["artifact_id"] == successor
        assert "from the open pane" in _rows(db, successor)
        assert _rows(db, artifact) == []
    finally:
        db.close()


def test_a_top_level_comment_from_a_pane_on_the_old_id_lands_on_the_successor(client, isolated_db):
    db = get_db()
    try:
        artifact, _root = _document_with_thread(db)
        successor = _promote(db, artifact)
        response = client.post(
            f"/api/artifacts/{artifact}/comments", params=TOKEN,
            json={"body": "whole-document note", "review": True, "delivery_policy": LANE},
        )
        assert response.status_code == 200, response.text
        assert "whole-document note" in _rows(db, successor)
        assert _rows(db, artifact) == []
    finally:
        db.close()


def test_a_second_promote_is_followed_to_the_live_document(client, isolated_db):
    db = get_db()
    try:
        artifact, root = _document_with_thread(db)
        first = _promote(db, artifact)
        last = _promote(db, first)
        response = _reply(client, artifact, root)
        assert response.status_code == 200, response.text
        assert "from the open pane" in _rows(db, last)
        assert _rows(db, artifact) == _rows(db, first) == []
    finally:
        db.close()


def test_a_reply_is_idempotent_across_a_promote(client, isolated_db):
    """The pane resends a draft with its client id when the first answer was lost: it is one comment."""
    db = get_db()
    try:
        artifact, root = _document_with_thread(db)
        body = {"body": "once", "parent_id": root["id"], "delivery_policy": LANE, "client_id": "draft-1"}
        assert client.post(f"/api/artifacts/{artifact}/comments", params=TOKEN, json=body).status_code == 200
        successor = _promote(db, artifact)
        again = client.post(f"/api/artifacts/{artifact}/comments", params=TOKEN, json=body)
        assert again.status_code == 200, again.text
        assert _rows(db, successor).count("once") == 1
    finally:
        db.close()


def test_a_write_is_refused_when_the_chain_ends_in_a_retired_document(client, isolated_db):
    db = get_db()
    try:
        artifact, root = _document_with_thread(db)
        successor = _promote(db, artifact)
        models.retire(successor, tombstone=True, cause="file_gone", db=db)
        before = _rows(db, successor)
        response = _reply(client, artifact, root)
        assert (response.status_code, response.json()["detail"]) == (409, NOT_COMMENTABLE)
        assert _rows(db, artifact) == [] and _rows(db, successor) == before
    finally:
        db.close()


def test_a_write_is_refused_when_the_caller_cannot_use_the_successor(client, isolated_db):
    db = get_db()
    try:
        artifact, root = _document_with_thread(db)
        seed_thread(db, "stranger")
        db.execute("UPDATE threads SET user_id=? WHERE id='stranger'", (OTHER_USER,))
        successor = _promote(db, artifact, owner="stranger")
        before = _rows(db, successor)
        response = _reply(client, artifact, root)
        assert (response.status_code, response.json()["detail"]) == (409, NOT_COMMENTABLE)
        assert _rows(db, successor) == before
    finally:
        db.close()


def test_a_deleted_documents_identity_still_takes_comments_as_before(client, isolated_db):
    """A retirement with no successor is not a promote: nothing to follow, nothing changes."""
    db = get_db()
    try:
        artifact, root = _document_with_thread(db)
        models.retire(artifact, tombstone=True, cause="file_gone", db=db)
        response = _reply(client, artifact, root)
        assert response.status_code == 200, response.text
        assert "from the open pane" in _rows(db, artifact)
    finally:
        db.close()


def test_a_promote_between_the_check_and_the_write_refuses_instead_of_writing_to_the_retired_row(
    client, isolated_db, monkeypatch,
):
    """The route's check can run before a promote commits; the insert re-reads the identity inside its own transaction."""
    import api.doc_comment_access as routes

    db = get_db()
    try:
        artifact, root = _document_with_thread(db)
        successor = _promote(db, artifact)
        monkeypatch.setattr(routes, "_live_artifact_id", lambda artifact_id, user_id, db: artifact_id)
        before = _rows(db, successor)
        response = _reply(client, artifact, root)
        assert response.status_code == 404, response.text
        assert _rows(db, artifact) == [] and _rows(db, successor) == before
    finally:
        db.close()


def test_positions_from_a_pane_on_the_old_id_update_the_moved_comments(client, isolated_db):
    db = get_db()
    try:
        artifact, root = _document_with_thread(db)
        successor = _promote(db, artifact)
        response = client.post(
            f"/api/artifacts/{artifact}/comments/positions", params=TOKEN,
            json={"updates": [{"id": root["id"], "from_off": 3, "to_off": 9}]},
        )
        assert (response.status_code, response.json()["updated"]) == (200, 1), response.text
        row = db.execute("SELECT artifact_id, from_off, to_off FROM doc_comments WHERE id=?", (root["id"],)).fetchone()
        assert (row["artifact_id"], row["from_off"], row["to_off"]) == (successor, 3, 9)
    finally:
        db.close()


def test_a_thread_viewed_stamp_from_a_pane_on_the_old_id_is_recorded(client, isolated_db):
    db = get_db()
    try:
        artifact, root = _document_with_thread(db)
        reply, _ = add_comment(db, artifact, author_id="owner", author="Owner", parent_id=root["id"])
        _promote(db, artifact)
        response = client.post(
            f"/api/artifacts/{artifact}/comments/{root['id']}/viewed",
            headers=HEADERS, json={"seen_reply_id": reply["id"]},
        )
        assert response.status_code == 200, response.text
    finally:
        db.close()


def test_held_feedback_actions_from_a_pane_on_the_old_id_reach_the_carried_batches(client, isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        db.execute("UPDATE threads SET user_id=? WHERE id='owner'", (OWNER_USER,))
        artifact = seed_document(db, "owner")
        add_comment(db, artifact, author_kind="user", author="Steve", delivery_policy="document-held")
        _promote(db, artifact)
        response = client.post(
            "/api/doc-feedback/touch", headers=HEADERS,
            json={"artifact_id": artifact, "delivery_policy": "document-held"},
        )
        assert (response.status_code, response.json()["touched"]) == (200, 1), response.text
    finally:
        db.close()


def test_accept_all_from_a_pane_on_a_document_promoted_nowhere_is_refused(client, isolated_db):
    db = get_db()
    try:
        artifact, _root = _document_with_thread(db)
        successor = _promote(db, artifact)
        models.retire(successor, tombstone=True, cause="file_gone", db=db)
        response = client.post(f"/api/artifacts/{artifact}/suggestions/accept-all", params=TOKEN)
        assert (response.status_code, response.json()["detail"]) == (409, NOT_COMMENTABLE)
    finally:
        db.close()


def test_accept_all_gives_a_caller_who_may_not_use_the_document_the_same_refusal_whether_or_not_it_was_promoted(
    client, isolated_db,
):
    """A foreign id stays non-enumerable: promoted or unknown, the answer is the same 404."""
    db = get_db()
    try:
        seed_thread(db, "stranger")
        db.execute("UPDATE threads SET user_id=? WHERE id='stranger'", (OTHER_USER,))
        foreign = seed_document(db, "stranger")
        _promote(db, foreign, owner="stranger")
        for artifact in (foreign, "no-such-identity"):
            response = client.post(f"/api/artifacts/{artifact}/suggestions/accept-all", params=TOKEN)
            assert response.status_code == 404, (artifact, response.text)
    finally:
        db.close()


def test_accept_all_leaves_a_suggestion_on_an_earlier_identity_open_listed_marked_and_decidable_alone(
    client, isolated_db,
):
    """Approve all covers the current document's suggestions; one left on an earlier location stays open."""
    db = get_db()
    try:
        artifact, root = _document_with_thread(db)
        successor = _promote(db, artifact)
        stray, _ = add_comment(db, artifact, author_kind="user", author="Steve", replacement="a rewrite")
        response = client.post(f"/api/artifacts/{successor}/suggestions/accept-all", params=TOKEN)
        assert response.status_code == 200, response.text
        assert response.json()["accepted"] == []
        listed = {t["id"]: t for t in client.get(f"/api/artifacts/{successor}/comments", params=TOKEN).json()["comments"]}
        assert listed[stray["id"]]["earlier_location"] is True and listed[stray["id"]]["decision"] is None
        assert listed[root["id"]]["earlier_location"] is False
        rejected = client.post(f"/api/comments/{stray['id']}/decision", params=TOKEN, json={"decision": "reject"})
        assert rejected.status_code == 200, rejected.text
    finally:
        db.close()


def test_the_startup_line_counts_retired_identities_holding_comments_and_held_batches_on_them(isolated_db):
    from registry.retirement_state import lineage_counts, log_lineage_counts

    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact = seed_document(db, "owner")
        add_comment(db, artifact, author_kind="user", author="Steve", delivery_policy="document-held")
        assert lineage_counts(db) == (0, 0)
        _promote(db, artifact)  # the promote carries the comment and the batch to the successor
        assert lineage_counts(db) == (0, 0)
        add_comment(db, artifact, author_kind="user", author="Steve", delivery_policy="document-held")
        assert lineage_counts(db) == (1, 1)
        lines: list[str] = []

        class _Log:
            def info(self, message, *args):
                lines.append(message.format(*args))

            def warning(self, message, *args):
                lines.append(message.format(*args))

        log_lineage_counts(_Log())
        assert lines == [
            "document lineage: retired_identities_with_comments=1 held_feedback_batches_on_retired_identities=1"
        ]
    finally:
        db.close()


def test_live_identity_follows_a_twelve_step_chain(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        chain = [seed_document(db, "owner") for _ in range(13)]
        for source, target in zip(chain, chain[1:]):
            db.execute(
                "UPDATE artifact_identities SET state='tombstone', metadata=json_set(metadata, '$.successor_id', ?) WHERE id=?",
                (target, source),
            )
        assert models.live_identity(chain[0], db=db) == chain[-1]
        assert sorted(models.predecessors(chain[-1], db=db)) == sorted(chain[:-1])
    finally:
        db.close()


def test_live_identity_follows_one_hop_and_stops_at_a_loop(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        first, second = seed_document(db, "owner"), seed_document(db, "owner")
        assert models.live_identity(first, db=db) == first
        assert models.live_identity("no-such-identity", db=db) == "no-such-identity"
        for source, target in ((first, second), (second, first)):
            db.execute(
                "UPDATE artifact_identities SET state='tombstone', metadata=json_set(metadata, '$.successor_id', ?) WHERE id=?",
                (target, source),
            )
        assert models.live_identity(first, db=db) is None
    finally:
        db.close()


def _listed(client, artifact):
    response = client.get(f"/api/artifacts/{artifact}/comments", params=TOKEN)
    assert response.status_code == 200, response.text
    return [thread["body"] for thread in response.json()["comments"]]


def test_the_list_a_pane_on_the_old_id_reads_shows_the_comments_on_the_live_document(client, isolated_db):
    db = get_db()
    try:
        artifact, root = _document_with_thread(db)
        before = _listed(client, artifact)
        assert before and root["body"] in before
        successor = _promote(db, artifact)
        assert _listed(client, artifact) == _listed(client, successor) == before
        assert _reply(client, artifact, root).status_code == 200
        thread = client.get(f"/api/artifacts/{artifact}/comments", params=TOKEN).json()["comments"][0]
        assert "from the open pane" in [reply["body"] for reply in thread["replies"]]
    finally:
        db.close()


def test_every_pane_of_a_document_reads_the_same_lineage_including_comments_saved_on_retired_ids(client, isolated_db):
    """A write saved on a retired row before any promote carried comments is still part of the document."""
    db = get_db()
    try:
        artifact, root = _document_with_thread(db)
        successor = _promote(db, artifact)
        stray, _ = add_comment(db, artifact, author_kind="user", author="Steve")
        stray_reply, _ = add_comment(db, artifact, author_kind="user", author="Steve", parent_id=stray["id"])
        stale = client.get(f"/api/artifacts/{artifact}/comments", params=TOKEN).json()["comments"]
        assert [thread["id"] for thread in stale] == [root["id"], stray["id"]]
        assert [reply["id"] for reply in stale[1]["replies"]] == [stray_reply["id"]]
        live = client.get(f"/api/artifacts/{successor}/comments", params=TOKEN).json()["comments"]
        assert [thread["id"] for thread in live] == [thread["id"] for thread in stale]
    finally:
        db.close()


def test_a_lineage_of_three_ids_reads_the_same_from_each_and_a_stray_thread_can_be_viewed(client, isolated_db):
    db = get_db()
    try:
        artifact, root = _document_with_thread(db)
        first = _promote(db, artifact)
        last = _promote(db, first)
        stray, _ = add_comment(db, first, author_kind="user", author="Steve")
        stray_reply, _ = add_comment(db, first, author_id="owner", author="Owner", parent_id=stray["id"])
        listed = [_listed(client, name) for name in (artifact, first, last)]
        assert listed[0] == listed[1] == listed[2] and len(listed[0]) == 2
        for pane in (artifact, last):
            response = client.post(
                f"/api/artifacts/{pane}/comments/{stray['id']}/viewed",
                headers=HEADERS, json={"seen_reply_id": stray_reply["id"]},
            )
            assert response.status_code == 200, (pane, response.text)
        response = client.post(f"/api/artifacts/{artifact}/comments/dismiss-all", headers=HEADERS)
        assert response.status_code == 200, response.text
        threads = client.get(f"/api/artifacts/{artifact}/comments", params=TOKEN).json()["comments"]
        assert all(not thread["new_reply_ids"] and not thread["new_comment"] for thread in threads)
    finally:
        db.close()


def test_the_list_on_the_old_id_reads_as_before_when_no_live_document_can_be_read(client, isolated_db):
    db = get_db()
    try:
        artifact, _root = _document_with_thread(db)
        successor = _promote(db, artifact)
        models.retire(successor, tombstone=True, cause="file_gone", db=db)
        assert _listed(client, artifact) == []
    finally:
        db.close()
