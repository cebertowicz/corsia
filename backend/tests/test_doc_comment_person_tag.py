"""An agent's tag of the person: stored, returned as a chip, and it marks the thread as needing them."""

import json

import pytest

from auth_fixtures import OWNER_TOKEN, OWNER_USER
from delegation_docs_fixtures import _OWNER, _mk_thread, _registrable_roots, doc

pytestmark = pytest.mark.integration


def _listed(integration_client, artifact_id: str) -> dict[str, dict]:
    listed = integration_client.get(f"/api/artifacts/{artifact_id}/comments", params={"token": OWNER_TOKEN})
    assert listed.status_code == 200, listed.text
    return {thread["id"]: thread for thread in listed.json()["comments"]}


def _stamp_viewed(integration_client, artifact_id: str, root_id: str, seen_id: str) -> None:
    stamped = integration_client.post(
        f"/api/artifacts/{artifact_id}/comments/{root_id}/viewed",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"}, json={"seen_reply_id": seen_id},
    )
    assert stamped.status_code == 200, stamped.text


def _tag_rows(comment_id: str) -> list:
    from infra.database import get_db

    db = get_db()
    try:
        return db.execute(
            "SELECT agent_id,agent_name FROM doc_comment_tags WHERE comment_id=? ORDER BY agent_id",
            (comment_id,),
        ).fetchall()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_an_agent_tags_the_person_on_a_new_comment_and_a_reply(integration_client, doc, _registrable_roots):
    from tools.documents import DocCommentCreateTool

    writer = _mk_thread(integration_client, _OWNER, title="Writer")
    root = json.loads(await DocCommentCreateTool().execute(
        doc_path=str(doc), quote="A unique tail sentence", body="Needs your call",
        tag_person=True, _thread_id=writer,
    ))["comment"]
    reply = json.loads(await DocCommentCreateTool().execute(
        parent_id=root["id"], body="Waiting on your decision",
        tag_person=True, _thread_id=writer,
    ))["comment"]

    # The chip carries the person's own name, the same name their comments carry as author.
    assert root["tagged_agents"] == [{"id": "ui", "name": OWNER_USER}]
    assert reply["tagged_agents"] == [{"id": "ui", "name": OWNER_USER}]
    # The person is never an agent tag id, which is the field that drives delivery.
    assert root["tagged_agent_ids"] == [] and reply["tagged_agent_ids"] == []
    assert [(row["agent_id"], row["agent_name"]) for row in _tag_rows(root["id"])] == [("ui", OWNER_USER)]
    assert [(row["agent_id"], row["agent_name"]) for row in _tag_rows(reply["id"])] == [("ui", OWNER_USER)]


@pytest.mark.asyncio
async def test_a_tagged_top_level_comment_needs_them_until_viewed(integration_client, doc, _registrable_roots):
    from tools.documents import DocCommentCreateTool

    writer = _mk_thread(integration_client, _OWNER, title="Writer")
    root = json.loads(await DocCommentCreateTool().execute(
        doc_path=str(doc), quote="A unique tail sentence", body="Needs your call",
        tag_person=True, _thread_id=writer,
    ))["comment"]

    # The person never commented on this document, so the ordinary rule would not mark it.
    assert _listed(integration_client, root["artifact_id"])[root["id"]]["new_comment"] is True

    _stamp_viewed(integration_client, root["artifact_id"], root["id"], root["id"])
    assert _listed(integration_client, root["artifact_id"])[root["id"]]["new_comment"] is False


@pytest.mark.asyncio
async def test_a_tagged_reply_in_an_untouched_thread_needs_them_until_viewed(integration_client, doc, _registrable_roots):
    from tools.documents import DocCommentCreateTool

    writer = _mk_thread(integration_client, _OWNER, title="Writer")
    root = json.loads(await DocCommentCreateTool().execute(
        doc_path=str(doc), quote="A unique tail sentence", body="An agent's own thread", _thread_id=writer,
    ))["comment"]
    reply = json.loads(await DocCommentCreateTool().execute(
        parent_id=root["id"], body="Needs your call",
        tag_person=True, _thread_id=writer,
    ))["comment"]

    threads = _listed(integration_client, root["artifact_id"])
    assert threads[root["id"]]["new_comment"] is False
    assert threads[root["id"]]["new_reply_ids"] == [reply["id"]]

    _stamp_viewed(integration_client, root["artifact_id"], root["id"], reply["id"])
    assert _listed(integration_client, root["artifact_id"])[root["id"]]["new_reply_ids"] == []


@pytest.mark.asyncio
async def test_an_untagged_agent_comment_still_needs_nobody(integration_client, doc, _registrable_roots):
    from tools.documents import DocCommentCreateTool

    writer = _mk_thread(integration_client, _OWNER, title="Writer")
    root = json.loads(await DocCommentCreateTool().execute(
        doc_path=str(doc), quote="A unique tail sentence", body="An ordinary note", _thread_id=writer,
    ))["comment"]
    reply = json.loads(await DocCommentCreateTool().execute(
        parent_id=root["id"], body="An ordinary reply", _thread_id=writer,
    ))["comment"]

    threads = _listed(integration_client, root["artifact_id"])
    assert threads[root["id"]]["new_comment"] is False
    assert threads[root["id"]]["new_reply_ids"] == []
    assert reply["tagged_agents"] == []


@pytest.mark.asyncio
async def test_a_person_tag_never_becomes_an_agent_recipient(integration_client, doc, _registrable_roots):
    from doceditor import feedback_comments
    from infra.database import get_db
    from tools.documents import DocCommentCreateTool

    owner = _mk_thread(integration_client, _OWNER, title="Doc owner")
    writer = _mk_thread(integration_client, _OWNER, title="Writer")
    root = json.loads(await DocCommentCreateTool().execute(
        doc_path=str(doc), quote="A unique tail sentence", body="For the owner", tag_person=True, _thread_id=owner,
    ))["comment"]
    reply = json.loads(await DocCommentCreateTool().execute(
        parent_id=root["id"], body="Also for the owner",
        tag_person=True, _thread_id=writer,
    ))["comment"]
    # A later comment in the same thread, with no tag of its own, must not inherit the person tag either.
    plain = json.loads(await DocCommentCreateTool().execute(
        parent_id=root["id"], body="Just the owner here", _thread_id=writer,
    ))["comment"]

    db = get_db()
    try:
        assert feedback_comments.recipients(db, reply) == (owner,)
        assert feedback_comments.recipients(db, plain) == (owner,)
        # The integration database is shared across the session, so scope this to the test's own
        # comments: none of them may open a delivery batch addressed to the person.
        person_batches = db.execute(
            """SELECT b.id,b.author_kind,b.author_id,i.comment_id FROM doc_feedback_batches b
               JOIN doc_feedback_batch_items i ON i.batch_id=b.id
               WHERE b.recipient_id=? AND i.comment_id IN (?,?,?)""",
            (feedback_comments.USER_AUTHOR_ID, root["id"], reply["id"], plain["id"]),
        ).fetchall()
        assert not person_batches, [dict(row) for row in person_batches]
        assert db.execute(
            "SELECT COUNT(*) FROM doc_feedback_batch_items WHERE comment_id=?", (reply["id"],)
        ).fetchone()[0] == 1
    finally:
        db.close()
