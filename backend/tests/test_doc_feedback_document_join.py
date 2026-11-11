"""A new top-level comment joins the document's open task its owner has not started."""

from __future__ import annotations

from pathlib import Path

from doc_feedback_helpers import add_comment, add_person_comment, batches, feedback_nodes, seed_document, seed_thread
from doceditor import comment_actions, comments, feedback_batch_edits, feedback_batches
from infra.database import get_db, write_transaction
from plans import holds, service
from plans.feedback_nodes import AGENT_FRAMING, USER_FRAMING

_HANDED = "SELECT 1 FROM plan_node_attempts WHERE node_id=?"


def _agent_comment(db, artifact_id: str, author_id: str = "author", author: str = "Reviewer", **comment) -> dict:
    row, _ = add_comment(db, artifact_id, author_id=author_id, author=author, **comment)
    feedback_batches.close_batch(db, batches(db, state="open")[0]["id"], "quiet")
    return row


def _queued_document(db) -> tuple[str, dict, dict]:
    """A document whose owner is busy with the first comment's task while the second comment's task waits."""
    seed_thread(db, "owner")
    seed_thread(db, "author", title="Reviewer")
    seed_thread(db, "second", title="Editor")
    artifact_id = seed_document(db, "owner")
    _agent_comment(db, artifact_id)
    queued_comment = _agent_comment(db, artifact_id)
    busy, queued = feedback_nodes(db, "owner")
    assert db.execute(_HANDED, (busy["id"],)).fetchone() is not None
    assert db.execute(_HANDED, (queued["id"],)).fetchone() is None
    return artifact_id, queued_comment, queued


def test_comments_before_the_owner_starts_share_one_task_that_settles_with_the_last(isolated_db):
    db = get_db()
    try:
        artifact_id, second, queued = _queued_document(db)

        third = _agent_comment(db, artifact_id)

        assert [node["id"] for node in feedback_nodes(db, "owner")][1:] == [queued["id"]]
        joined = service.get_node(queued["id"], db=db)
        assert f"Comment {third['id']}" in joined.description
        assert joined.description.startswith(queued["description"])
        assert "left 2 comments on" in joined.title

        with write_transaction(db, scope="test_document_join_settlement") as connection:
            comment_actions.record_decision(
                second["id"], decision="accept", decided_by="Robin", decided_by_thread_id=None, db=connection,
            )
        comments.update(second["id"], {"resolved": True}, db=db)
        assert service.get_node(queued["id"], db=db).state != "accepted"

        comments.update(third["id"], {"resolved": True}, db=db)
        assert service.get_node(queued["id"], db=db).state == "accepted"
    finally:
        db.close()


def test_person_comments_before_the_owner_starts_share_one_task_with_restated_intent(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact_id = seed_document(db, "owner")
        add_person_comment(db, artifact_id)
        add_person_comment(db, artifact_id)
        _, queued = feedback_nodes(db, "owner")

        third, _ = add_person_comment(db, artifact_id)

        assert len(feedback_nodes(db, "owner")) == 2
        joined = service.get_node(queued["id"], db=db)
        assert f"Comment {third['id']}" in joined.description
        assert joined.intent == queued["intent"].replace("1 comment on", "2 comments on")
        assert "2 comments on" in joined.title
    finally:
        db.close()


def test_comment_after_the_task_was_handed_gets_its_own_task(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact_id = seed_document(db, "owner")
        add_person_comment(db, artifact_id)
        (first,) = feedback_nodes(db, "owner")
        assert db.execute(_HANDED, (first["id"],)).fetchone() is not None

        later, _ = add_person_comment(db, artifact_id)

        nodes = feedback_nodes(db, "owner")
        assert len(nodes) == 2 and nodes[1]["id"] != first["id"]
        assert service.get_node(first["id"], db=db).description == first["description"]
        assert f"Comment {later['id']}" in service.get_node(nodes[1]["id"], db=db).description
        assert db.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id='owner' AND id LIKE 'plan-wake:intent_input:%'",
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_comment_does_not_join_a_waiting_task(isolated_db):
    db = get_db()
    try:
        artifact_id, _, queued = _queued_document(db)
        holds.pause(
            queued["id"], "test", expected_revision=queued["revision"], actor_id="owner",
            provenance="test", db=db,
        )
        db.commit()

        _agent_comment(db, artifact_id)

        nodes = feedback_nodes(db, "owner")
        assert len(nodes) == 3 and nodes[2]["id"] != queued["id"]
        assert service.get_node(queued["id"], db=db).description == queued["description"]
    finally:
        db.close()


def test_comment_on_another_document_gets_its_own_task(isolated_db):
    db = get_db()
    try:
        artifact_id, _, queued = _queued_document(db)
        other = seed_document(db, "owner")

        elsewhere = _agent_comment(db, other)

        nodes = feedback_nodes(db, "owner")
        assert len(nodes) == 3 and nodes[2]["id"] != queued["id"]
        assert service.get_node(queued["id"], db=db).description == queued["description"]
        assert f"Comment {elsewhere['id']}" in service.get_node(nodes[2]["id"], db=db).description
        assert artifact_id != other
    finally:
        db.close()


def test_comments_from_two_authors_give_a_title_that_names_both(isolated_db):
    db = get_db()
    try:
        artifact_id, _, queued = _queued_document(db)
        assert service.get_node(queued["id"], db=db).title.startswith("Reviewer left 1 comment on")

        _agent_comment(db, artifact_id, author_id="second", author="Editor")

        assert len(feedback_nodes(db, "owner")) == 2
        title = service.get_node(queued["id"], db=db).title
        assert title.startswith("Reviewer and Editor left 2 comments on")
    finally:
        db.close()


def test_a_suggestion_still_gets_its_own_task(isolated_db):
    db = get_db()
    try:
        artifact_id, _, queued = _queued_document(db)

        _agent_comment(db, artifact_id, replacement="New")

        nodes = feedback_nodes(db, "owner")
        assert len(nodes) == 3 and nodes[2]["id"] != queued["id"]
    finally:
        db.close()


def test_a_persons_comment_joins_an_agents_task_and_states_the_intent(isolated_db):
    db = get_db()
    try:
        artifact_id, _, queued = _queued_document(db)
        assert queued["intent"] is None

        person, _ = add_person_comment(db, artifact_id)

        assert len(feedback_nodes(db, "owner")) == 2
        joined = service.get_node(queued["id"], db=db)
        assert joined.title.startswith("Reviewer and Steve left 2 comments on")
        assert joined.intent.startswith("Answer the person's 1 comment on ")
        assert f"Comment {person['id']}" in joined.description
        assert joined.description.startswith(queued["description"])
        assert USER_FRAMING in joined.description
        assert joined.description.count(AGENT_FRAMING) == 1
    finally:
        db.close()


def test_an_agents_comment_joins_a_persons_task_without_dropping_its_intent(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author", title="Reviewer")
        artifact_id = seed_document(db, "owner")
        add_person_comment(db, artifact_id)
        add_person_comment(db, artifact_id)
        _, queued = feedback_nodes(db, "owner")
        assert queued["intent"].startswith("Answer the person's 1 comment on ")

        agent = _agent_comment(db, artifact_id)

        assert len(feedback_nodes(db, "owner")) == 2
        joined = service.get_node(queued["id"], db=db)
        assert joined.title.startswith("Steve and Reviewer left 2 comments on")
        assert joined.intent == queued["intent"]
        assert f"Comment {agent['id']}" in joined.description
        assert joined.description.startswith(queued["description"])
        assert AGENT_FRAMING in joined.description
        assert joined.description.count(USER_FRAMING) == 1
    finally:
        db.close()


def _agent_edit(db, artifact_id: str) -> None:
    with write_transaction(db, scope="test_document_join_edit") as connection:
        feedback_batch_edits.record_edit(
            connection, artifact_id=artifact_id, recipient_id="owner", author_kind="agent", author_id="author",
            actor_display="Reviewer", path=Path("/workspace/shared/Design.md"), before=b"old\n", after=b"new\n",
        )
    feedback_batches.close_batch(db, batches(db, state="open")[0]["id"], "quiet")


def test_an_agents_comment_joining_its_own_edit_only_task_brings_the_framing(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author", title="Reviewer")
        artifact_id = seed_document(db, "owner")
        _agent_comment(db, artifact_id)
        _agent_edit(db, artifact_id)
        _, queued = feedback_nodes(db, "owner")
        assert AGENT_FRAMING not in queued["description"]

        _agent_comment(db, artifact_id)

        assert len(feedback_nodes(db, "owner")) == 2
        assert service.get_node(queued["id"], db=db).description.count(AGENT_FRAMING) == 1
    finally:
        db.close()


def test_a_persons_comment_joining_their_own_edit_only_task_brings_the_framing(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact_id = seed_document(db, "owner")
        add_person_comment(db, artifact_id)
        with write_transaction(db, scope="test_document_join_edit") as connection:
            batch_id = feedback_batch_edits.record_edit(
                connection, artifact_id=artifact_id, recipient_id="owner", author_kind="user",
                author_id=feedback_batches.USER_AUTHOR_ID, actor_display="Steve",
                path=Path("/workspace/shared/Design.md"), before=b"old\n", after=b"new\n",
            )
        feedback_batches.deliver_person_comment(db, [batch_id], artifact_id)
        _, queued = feedback_nodes(db, "owner")
        assert USER_FRAMING not in queued["description"]

        add_person_comment(db, artifact_id)

        assert len(feedback_nodes(db, "owner")) == 2
        joined = service.get_node(queued["id"], db=db)
        assert joined.description.count(USER_FRAMING) == 1
        assert joined.title.startswith("Steve edited ") and joined.title.endswith(" and left 1 comment")
    finally:
        db.close()


def test_a_later_comment_from_an_earlier_author_keeps_the_names_in_first_seen_order(isolated_db):
    db = get_db()
    try:
        artifact_id, _, queued = _queued_document(db)

        _agent_comment(db, artifact_id, author_id="second", author="Editor")
        _agent_comment(db, artifact_id)

        assert len(feedback_nodes(db, "owner")) == 2
        assert service.get_node(queued["id"], db=db).title.startswith("Reviewer and Editor left 3 comments on")
    finally:
        db.close()
