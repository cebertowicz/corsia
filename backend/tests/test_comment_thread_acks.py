"""Each person and agent acknowledges a comment thread separately; the card shows who has; the thread
resolves once everyone who commented on or is tagged into it has acknowledged it; an agent's
acknowledgement stops its wakes for that thread."""

from __future__ import annotations

import json

import pytest

from auth_fixtures import OWNER_TOKEN
from delegation_docs_fixtures import _OWNER, _mk_thread, _registrable_roots, doc  # noqa: F401

pytestmark = pytest.mark.integration

QUOTE = "A unique tail sentence"


async def _comment(thread_id, doc=None, **fields):
    from tools.documents import DocCommentCreateTool

    if doc is not None:
        fields = {"doc_path": str(doc), "quote": QUOTE, **fields}
    created = await DocCommentCreateTool().execute(_thread_id=thread_id, **fields)
    return json.loads(created)["comment"]


async def _ack(thread_id, comment_id, acked=True):
    from tools.documents import DocCommentUpdateTool

    return await DocCommentUpdateTool().execute(comment_id=comment_id, acked=acked, _thread_id=thread_id)


def _person_ack(client, comment_id, acked=True):
    response = client.patch(f"/api/comments/{comment_id}", params={"token": OWNER_TOKEN}, json={"acked": acked})
    assert response.status_code == 200, response.text
    return response.json()["comment"]


def _row(comment_id):
    from doceditor import comments as store

    return store.get_comment(comment_id)


def _woken(comment_id):
    """Who a comment's feedback batches were addressed to."""
    from infra.database import get_db

    db = get_db()
    try:
        return sorted(row["recipient_id"] for row in db.execute(
            """SELECT DISTINCT b.recipient_id FROM doc_feedback_batches b
               JOIN doc_feedback_batch_items i ON i.batch_id=b.id WHERE i.comment_id=?""", (comment_id,)))
    finally:
        db.close()


async def _owned_thread(client, doc):
    """An owner registers ``doc``; a reviewer comments on it; the owner replies."""
    from tools.artifacts import ArtifactRegisterTool

    owner = _mk_thread(client, _OWNER, title="Doc Owner")
    await ArtifactRegisterTool().execute(path=str(doc), _thread_id=owner)
    reviewer = _mk_thread(client, _OWNER, title="Reviewer")
    root = await _comment(reviewer, doc, body="Tighten this.")
    await _comment(owner, parent_id=root["id"], body="Will do.")
    return owner, reviewer, root["id"]


def _make_taggable(thread_id):
    from infra.database import get_db

    db = get_db()
    try:
        db.execute("UPDATE threads SET agent_enabled=1, agent_responsibility='Review' WHERE id=?", (thread_id,))
        db.commit()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_one_participant_acknowledging_leaves_the_thread_open_and_shows_who(integration_client, doc, _registrable_roots):
    owner, _reviewer, root = await _owned_thread(integration_client, doc)

    await _ack(owner, root)

    row = _row(root)
    assert row["resolved"] == 0
    assert row["acknowledged_by"] == [{"id": owner, "name": "Doc Owner", "kind": "agent"}]


@pytest.mark.asyncio
async def test_two_participants_acknowledging_resolves_the_thread(integration_client, doc, _registrable_roots):
    from doceditor import comments as store

    owner, reviewer, root = await _owned_thread(integration_client, doc)
    await _ack(owner, root)
    assert _row(root)["resolved"] == 0

    # Acknowledging from a reply acknowledges its whole thread.
    thread, = (t for t in store.list_for_artifact(_row(root)["artifact_id"]) if t["id"] == root)
    result = json.loads(await _ack(reviewer, thread["replies"][0]["id"]))

    assert result["comment"]["parent_id"] == root
    row = _row(root)
    assert row["resolved"] == 1
    assert [ack["name"] for ack in row["acknowledged_by"]] == ["Doc Owner", "Reviewer"]


@pytest.fixture
def _fresh_turns():
    from doceditor import writer_turns

    writer_turns.reset()
    yield
    writer_turns.reset()


def _notes(thread_id):
    from infra.database import get_db

    db = get_db()
    try:
        return [row["summary"] for row in db.execute(
            "SELECT summary FROM context_change_journal WHERE object_kind='thread_context' AND object_id=? ORDER BY seq",
            (thread_id,))]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_the_author_hears_only_resolved_when_an_agree_completes_its_thread(integration_client, doc, _registrable_roots):
    owner, reviewer, root = await _owned_thread(integration_client, doc)
    await _ack(reviewer, root)

    await _ack(owner, root)

    assert _row(root)["resolved"] == 1
    assert _notes(reviewer) == [f"Doc Owner resolved your comment on {doc.stem}."]


@pytest.mark.asyncio
async def test_the_last_agree_resolves_at_once_even_while_a_person_holds_the_writer_turn(integration_client, doc, _registrable_roots, _fresh_turns):
    from doceditor import writer_turns

    owner, reviewer, root = await _owned_thread(integration_client, doc)
    await _ack(owner, root)
    writer_turns.take(object(), root, writer_turns.PersonHold("u1", "Steve", _row(root)["artifact_id"]))

    await _ack(reviewer, root)

    # Nothing waits on the in-memory turn, so a restart that drops it loses nothing.
    writer_turns.reset()
    assert _row(root)["resolved"] == 1


@pytest.mark.asyncio
async def test_a_reply_from_someone_who_has_not_agreed_reopens_a_thread_everyone_agreed(integration_client, doc, _registrable_roots, _fresh_turns):
    from doceditor import writer_turns

    owner, reviewer, root = await _owned_thread(integration_client, doc)
    other = _mk_thread(integration_client, _OWNER, title="Other")
    assert writer_turns.agent_claim(root, other)  # its reply was being written as the thread resolved
    await _ack(owner, root)
    await _ack(reviewer, root)
    assert _row(root)["resolved"] == 1

    await _comment(other, parent_id=root, body="One case this misses.")

    row = _row(root)
    assert row["resolved"] == 0 and row["acknowledged_by"] == []


@pytest.mark.asyncio
async def test_a_reply_from_someone_who_agreed_keeps_the_thread_resolved(integration_client, doc, _registrable_roots):
    owner, reviewer, root = await _owned_thread(integration_client, doc)
    await _ack(owner, root)
    await _ack(reviewer, root)

    await _comment(reviewer, parent_id=root, body="Thanks.")

    assert _row(root)["resolved"] == 1


@pytest.mark.asyncio
async def test_the_last_holdout_leaving_resolves_the_thread(integration_client, doc, _registrable_roots):
    from doceditor import comment_recipients
    from infra.database import get_db, write_transaction

    owner, reviewer, root = await _owned_thread(integration_client, doc)
    other = _mk_thread(integration_client, _OWNER, title="Other")
    await _comment(other, parent_id=root, body="Not sure.")
    await _ack(owner, root)
    await _ack(reviewer, root)
    assert _row(root)["resolved"] == 0

    db = get_db()
    try:
        with write_transaction(db, scope="test_leave") as connection:
            comment_recipients.leave(connection, _row(root), other)
    finally:
        db.close()

    assert _row(root)["resolved"] == 1


@pytest.mark.asyncio
async def test_a_tagged_agent_that_never_commented_counts(integration_client, doc, _registrable_roots):
    owner, reviewer, root = await _owned_thread(integration_client, doc)
    tagged = _mk_thread(integration_client, _OWNER, title="Tagged")
    _make_taggable(tagged)
    await _comment(reviewer, parent_id=root, body="What do you think?", tagged_agent_ids=[tagged])

    await _ack(owner, root)
    await _ack(reviewer, root)
    assert _row(root)["resolved"] == 0

    await _ack(tagged, root)
    assert _row(root)["resolved"] == 1


@pytest.mark.asyncio
async def test_an_acknowledged_agent_gets_no_wake_on_a_new_reply(integration_client, doc, _registrable_roots):
    owner, reviewer, root = await _owned_thread(integration_client, doc)
    before = await _comment(reviewer, parent_id=root, body="One more thing.")
    assert _woken(before["id"]) == [owner]

    await _ack(owner, root)
    after = await _comment(reviewer, parent_id=root, body="And another.")

    assert _woken(after["id"]) == []
    assert _row(root)["resolved"] == 0


@pytest.mark.asyncio
async def test_a_tag_brings_an_acknowledged_agent_back(integration_client, doc, _registrable_roots):
    owner, reviewer, root = await _owned_thread(integration_client, doc)
    _make_taggable(owner)
    await _ack(owner, root)

    asked = await _comment(reviewer, parent_id=root, body="Are you sure?", tagged_agent_ids=[owner])

    assert _woken(asked["id"]) == [owner]
    # Delivering the tag asks for its acknowledgement again.
    from doceditor import feedback_batches
    from infra.database import get_db

    db = get_db()
    try:
        batch_id, = (row["id"] for row in db.execute(
            """SELECT b.id FROM doc_feedback_batches b JOIN doc_feedback_batch_items i ON i.batch_id=b.id
               WHERE i.comment_id=? AND b.recipient_id=?""", (asked["id"], owner)))
        assert feedback_batches.close_batch(db, batch_id, "send")[0]
    finally:
        db.close()
    assert _row(root)["acknowledged_by"] == []


@pytest.mark.asyncio
async def test_the_persons_acknowledgement_counts(integration_client, doc, _registrable_roots):
    owner, _reviewer, root = await _owned_thread(integration_client, doc)
    person_reply = integration_client.post(
        f"/api/artifacts/{_row(root)['artifact_id']}/comments", params={"token": OWNER_TOKEN},
        json={"body": "Agreed with the change.", "parent_id": root, "delivery_policy": "document-held"})
    assert person_reply.status_code == 200, person_reply.text
    await _ack(owner, root)
    reviewer_id = _row(root)["author_thread_id"]
    await _ack(reviewer_id, root)
    assert _row(root)["resolved"] == 0

    row = _person_ack(integration_client, root)

    assert row["resolved"] == 1
    person = row["acknowledged_by"][-1]
    assert (person["id"], person["kind"]) == ("ui", "user") and person["name"]


@pytest.mark.asyncio
async def test_a_person_tag_makes_the_person_a_participant(integration_client, doc, _registrable_roots):
    owner, reviewer, root = await _owned_thread(integration_client, doc)
    await _comment(reviewer, parent_id=root, body="Steve should decide.", tag_person=True)

    await _ack(owner, root)
    await _ack(reviewer, root)
    assert _row(root)["resolved"] == 0

    assert _person_ack(integration_client, root)["resolved"] == 1


@pytest.mark.asyncio
async def test_taking_back_an_acknowledgement_and_reopening(integration_client, doc, _registrable_roots):
    owner, reviewer, root = await _owned_thread(integration_client, doc)
    await _ack(owner, root)
    await _ack(owner, root, acked=False)
    assert _row(root)["acknowledged_by"] == []

    await _ack(owner, root)
    await _ack(reviewer, root)
    assert _row(root)["resolved"] == 1

    # Re-opening takes back every acknowledgement, so the thread is open until everyone acknowledges again.
    reopened = integration_client.patch(
        f"/api/comments/{root}", params={"token": OWNER_TOKEN},
        json={"resolved": False, "delivery_policy": "document-held"})
    assert reopened.status_code == 200, reopened.text
    assert _row(root)["acknowledged_by"] == []
    await _ack(owner, root)
    assert _row(root)["resolved"] == 0


@pytest.mark.asyncio
async def test_an_open_suggestion_is_not_resolved_by_acknowledgements(integration_client, doc, _registrable_roots):
    from tools.artifacts import ArtifactRegisterTool

    owner = _mk_thread(integration_client, _OWNER, title="Doc Owner")
    await ArtifactRegisterTool().execute(path=str(doc), _thread_id=owner)
    reviewer = _mk_thread(integration_client, _OWNER, title="Reviewer")
    suggestion = await _comment(reviewer, doc, body="Shorter.", replacement="A tail")

    await _ack(reviewer, suggestion["id"])

    row = _row(suggestion["id"])
    assert row["resolved"] == 0 and row["decision"] is None
    assert [ack["name"] for ack in row["acknowledged_by"]] == ["Reviewer"]


def test_migration_drops_the_shared_flag_without_crediting_anyone():
    import sqlite3
    from contextlib import closing

    from infra.migration_183_to_184_comment_acks import migrate_183_to_184

    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE TABLE doc_comments (id TEXT PRIMARY KEY, body TEXT, resolved INTEGER DEFAULT 0, acked INTEGER DEFAULT 0)")
        db.execute("INSERT INTO doc_comments(id,body,acked) VALUES ('c1','Agreed thread',1)")

        migrate_183_to_184(db)
        migrate_183_to_184(db)

        assert "acked" not in {column[1] for column in db.execute("PRAGMA table_info(doc_comments)")}
        assert db.execute("SELECT id,body,resolved FROM doc_comments").fetchall() == [("c1", "Agreed thread", 0)]
        assert db.execute("SELECT COUNT(*) FROM doc_comment_acks").fetchone() == (0,)
