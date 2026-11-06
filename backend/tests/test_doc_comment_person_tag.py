"""An agent's tag of the person: stored, returned as a chip, and it marks the thread as needing them."""

import json

import pytest

from auth_fixtures import OTHER_TOKEN, OWNER_TOKEN, OWNER_USER
from delegation_docs_fixtures import _OWNER, _mk_thread, _post, _registrable_roots, doc

pytestmark = pytest.mark.integration


def _listed(integration_client, artifact_id: str, token: str = OWNER_TOKEN) -> dict[str, dict]:
    listed = integration_client.get(f"/api/artifacts/{artifact_id}/comments", params={"token": token})
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


def _person_mark(comment_id: str) -> str | None:
    from infra.database import get_db

    db = get_db()
    try:
        return db.execute("SELECT tagged_user_id FROM doc_comments WHERE id=?", (comment_id,)).fetchone()[0]
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

    # The tag is the comment's own mark, and the field that carries it names the person, not an agent.
    assert root["tagged_person"] == OWNER_USER and reply["tagged_person"] == OWNER_USER
    assert root["tagged_agents"] == [] and reply["tagged_agents"] == []
    assert root["tagged_agent_ids"] == [] and reply["tagged_agent_ids"] == []
    assert _person_mark(root["id"]) == OWNER_USER and _person_mark(reply["id"]) == OWNER_USER
    # The tag table stays agents only.
    assert _tag_rows(root["id"]) == [] and _tag_rows(reply["id"]) == []
    # The listing carries the same field, which is what the comment surface renders.
    listed = _listed(integration_client, root["artifact_id"])
    assert listed[root["id"]]["tagged_person"] == OWNER_USER


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
    assert reply["tagged_person"] is None
    assert _person_mark(reply["id"]) is None


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


@pytest.mark.asyncio
async def test_a_person_tag_on_a_suggestion_or_a_review_note_needs_them_and_reloads_the_surface(
    integration_client, doc, _registrable_roots, monkeypatch,
):
    """The tag beats the noise rules in both places that apply them, not just in the unread read.

    ``unread_comment_rows`` makes a person-tagged suggestion or review note eligible, and
    ``may_change_unread_attention`` has to say so too: the comment surface reloads on that hint
    alone, so a tag that only reached the SQL would stay invisible until something else refreshed.
    """
    from doceditor import comment_notifications
    from tools.documents import DocCommentCreateTool

    hints: list[bool] = []
    announce = comment_notifications.announce_after_commit

    def record(connection, artifact_id, **kwargs):
        hints.append(bool(kwargs.get("unread_attention_changed")))
        return announce(connection, artifact_id, **kwargs)

    monkeypatch.setattr(comment_notifications, "announce_after_commit", record)

    writer = _mk_thread(integration_client, _OWNER, title="Writer")
    suggestion = json.loads(await DocCommentCreateTool().execute(
        doc_path=str(doc), quote="A unique tail sentence", body="Your call on this wording",
        replacement="A unique tail sentence, rewritten.", tag_person=True, _thread_id=writer,
    ))["comment"]
    note = json.loads(await DocCommentCreateTool().execute(
        doc_path=str(doc), review=True, body="This needs your sign-off",
        tag_person=True, _thread_id=writer,
    ))["comment"]

    threads = _listed(integration_client, suggestion["artifact_id"])
    assert threads[suggestion["id"]]["new_comment"] is True
    assert threads[note["id"]]["new_comment"] is True
    assert hints == [True, True]


@pytest.mark.asyncio
async def test_an_agent_tag_and_a_person_tag_on_one_comment_stay_separate_fields(
    integration_client, doc, _registrable_roots, monkeypatch,
):
    """The event counts agents as it always did and reports the person's tag as its own fact."""
    from doceditor import live_events
    from tools.documents import DocCommentCreateTool

    rostered = _mk_thread(integration_client, _OWNER, title="Rostered reviewer")
    promoted = integration_client.patch(
        f"/api/agents/{rostered}",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
        json={"active": True, "responsibility": "Reviews the document."},
    )
    assert promoted.status_code == 200, promoted.text

    events: list[tuple] = []
    emit = live_events.emit

    def record(action, **properties):
        events.append((action, properties))
        return emit(action, **properties)

    monkeypatch.setattr(live_events, "emit", record)
    writer = _mk_thread(integration_client, _OWNER, title="Writer")
    comment = json.loads(await DocCommentCreateTool().execute(
        doc_path=str(doc), quote="A unique tail sentence", body="Both kinds of tag here",
        tagged_agent_ids=[rostered], tag_person=True, _thread_id=writer,
    ))["comment"]

    assert comment["tagged_agent_ids"] == [rostered]
    assert [agent["id"] for agent in comment["tagged_agents"]] == [rostered]
    assert comment["tagged_person"] == OWNER_USER
    # The tag table holds the agent row only; the person is the comment's own mark.
    assert [row["agent_id"] for row in _tag_rows(comment["id"])] == [rostered]
    assert ("comment_tagged", {
        "tag_count": 1, "person_tagged": True, "reply": False, "author_kind": "agent",
    }) in events


@pytest.mark.asyncio
async def test_a_tag_on_a_shared_document_needs_only_the_account_it_names(integration_client, doc, _registrable_roots):
    """Every account can read a `ui`-owned document; the tag names, and needs, the agent's own account only."""
    from infra.database import get_db
    from tools.documents import DocCommentCreateTool

    writer = _mk_thread(integration_client, _OWNER, title="Writer")
    root = json.loads(await DocCommentCreateTool().execute(
        doc_path=str(doc), quote="A unique tail sentence", body="Needs your call",
        tag_person=True, _thread_id=writer,
    ))["comment"]
    db = get_db()
    try:
        db.execute(
            "UPDATE artifact_registrations SET parent_thread_id='ui' WHERE artifact_id=? AND relationship_state LIKE 'owner-%'",
            (root["artifact_id"],),
        )
        db.commit()
    finally:
        db.close()

    thread = _listed(integration_client, root["artifact_id"])[root["id"]]
    assert thread["tagged_person"] == OWNER_USER
    assert thread["new_comment"] is True
    other = _listed(integration_client, root["artifact_id"], OTHER_TOKEN)[root["id"]]
    assert other["tagged_person"] == OWNER_USER
    assert other["new_comment"] is False


def test_the_corsia_doc_route_tags_the_person_too(integration_client, doc):
    writer = _mk_thread(integration_client, _OWNER, title="Writer")
    created = _post(integration_client, _OWNER, doc_path=str(doc), quote="A unique tail sentence",
                    body="Needs your call", tag_person=True, as_session_id=writer)
    assert created.status_code == 200, created.text
    comment = created.json()["comment"]

    assert comment["tagged_person"] == OWNER_USER
    assert _person_mark(comment["id"]) == OWNER_USER
    assert _tag_rows(comment["id"]) == []
