"""A reply to a comment joins the open task its thread already has, rather than making a second."""

from __future__ import annotations

from pathlib import Path

from doc_feedback_helpers import add_comment, add_person_comment, batches, seed_document, seed_thread
from doceditor import comment_actions, comments, feedback_batch_edits, feedback_batches
from infra.database import get_db, write_transaction
from plans import holds, service
from plans.feedback_nodes import create_feedback_node


def _person_reply(db, artifact_id: str, root_id: str, body: str = "Also the second point.") -> dict:
    row, feedback = add_comment(
        db, artifact_id, author_kind="user", author_id=None, author="Steve", parent_id=root_id,
    )
    db.execute("UPDATE doc_comments SET body=? WHERE id=?", (body, row["id"]))
    db.commit()
    feedback_batches.deliver_person_comment(db, feedback["batch_ids"], artifact_id)
    return row


def _feedback_nodes(db, thread_id: str) -> list:
    plan = service.ensure_plan("thread", thread_id, db=db)
    return db.execute(
        """SELECT DISTINCT n.* FROM plan_nodes n JOIN plan_node_artifact_links l ON l.node_id=n.id
           WHERE n.plan_id=? AND l.provenance='document.feedback_batch' ORDER BY n.created_at,n.id""",
        (plan.id,),
    ).fetchall()


def _queued_thread(db) -> tuple[str, dict, object]:
    """A document whose owner is busy with one comment's task while a second comment's task waits."""
    seed_thread(db, "owner")
    artifact_id = seed_document(db, "owner")
    add_person_comment(db, artifact_id)
    root, _ = add_person_comment(db, artifact_id)
    busy, first = _feedback_nodes(db, "owner")
    selected = "SELECT 1 FROM plan_node_attempts WHERE node_id=?"
    assert db.execute(selected, (busy["id"],)).fetchone() is not None
    assert db.execute(selected, (first["id"],)).fetchone() is None
    return artifact_id, root, first


def test_person_reply_joins_the_open_task_of_its_thread(isolated_db):
    db = get_db()
    try:
        artifact_id, root, first = _queued_thread(db)

        reply = _person_reply(db, artifact_id, root["id"])

        nodes = _feedback_nodes(db, "owner")
        assert [node["id"] for node in nodes][1:] == [first["id"]]
        joined = service.get_node(first["id"], db=db)
        assert "1 comment on" in first["title"]
        assert joined.title == first["title"].replace("1 comment on", "2 comments on")
        assert joined.requirements_revision == first["requirements_revision"] + 1
        assert db.execute(
            "SELECT 1 FROM plan_node_events WHERE node_id=? AND event='node_restated'", (first["id"],),
        ).fetchone() is not None
        assert f"Comment {reply['id']}" in joined.description
        assert "Feedback: Also the second point." in joined.description
        assert joined.description.startswith(first["description"])
        batch = db.execute(
            "SELECT delivered_node_id FROM doc_feedback_batches "
            "WHERE id IN (SELECT batch_id FROM doc_feedback_batch_items WHERE comment_id=?)",
            (reply["id"],),
        ).fetchone()
        assert batch["delivered_node_id"] == first["id"]
        words = db.execute(
            "SELECT source_id FROM plan_node_intent_words WHERE node_id=? ORDER BY added_at,rowid",
            (first["id"],),
        ).fetchall()
        assert [row["source_id"] for row in words] == [root["id"], reply["id"]]

        # Delivering the same batch again adds nothing.
        again = create_feedback_node(db, "owner", db.execute(
            "SELECT * FROM doc_feedback_batches WHERE delivered_node_id=? ORDER BY created_at DESC LIMIT 1",
            (first["id"],),
        ).fetchone())
        db.commit()
        assert again.node_id == first["id"]
        assert service.get_node(first["id"], db=db).description == joined.description
    finally:
        db.close()


def test_reply_to_a_task_its_owner_picked_up_gets_its_own_task(isolated_db):
    """The owner may have read the task already, so the reply is not hidden in its description."""
    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact_id = seed_document(db, "owner")
        root, _ = add_person_comment(db, artifact_id)
        (first,) = _feedback_nodes(db, "owner")

        _person_reply(db, artifact_id, root["id"])

        nodes = _feedback_nodes(db, "owner")
        assert len(nodes) == 2 and nodes[1]["id"] != first["id"]
        assert service.get_node(first["id"], db=db).description == first["description"]
    finally:
        db.close()


def test_next_reply_joins_the_queued_task_of_the_earlier_reply(isolated_db):
    """Past a task its owner picked up, the thread's next open task still takes the reply."""
    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact_id = seed_document(db, "owner")
        root, _ = add_person_comment(db, artifact_id)
        _person_reply(db, artifact_id, root["id"])
        first, queued = _feedback_nodes(db, "owner")

        later = _person_reply(db, artifact_id, root["id"], body="And the third.")

        nodes = _feedback_nodes(db, "owner")
        assert [node["id"] for node in nodes] == [first["id"], queued["id"]]
        assert f"Comment {later['id']}" in service.get_node(queued["id"], db=db).description
        assert service.get_node(first["id"], db=db).description == first["description"]
    finally:
        db.close()


def test_reply_after_the_task_finished_gets_its_own_task(isolated_db):
    db = get_db()
    try:
        artifact_id, root, first = _queued_thread(db)
        service.transition_node(first["id"], "closed", actor_id=None, provenance="test", db=db)
        db.commit()

        _person_reply(db, artifact_id, root["id"])

        nodes = _feedback_nodes(db, "owner")
        assert len(nodes) == 3 and nodes[2]["id"] != first["id"]
    finally:
        db.close()


def test_reply_to_a_parked_task_gets_its_own_task(isolated_db):
    """A parked task would not see the reply until its own wake, so the reply is not hidden in it."""
    db = get_db()
    try:
        artifact_id, root, first = _queued_thread(db)
        holds.pause(
            first["id"], "test", expected_revision=first["revision"], actor_id="owner",
            provenance="test", db=db,
        )
        db.commit()

        _person_reply(db, artifact_id, root["id"])

        assert len(_feedback_nodes(db, "owner")) == 3
    finally:
        db.close()


def _agent_batch(db, artifact_id: str, **comment) -> dict:
    row, _ = add_comment(db, artifact_id, **comment)
    feedback_batches.close_batch(db, batches(db, state="open")[0]["id"], "quiet")
    return row


def test_agent_task_holding_a_joined_reply_settles_only_once_the_reply_is_settled(isolated_db):
    """The task holds both batches, so settling the first alone must not close it with the reply unhandled."""
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author", title="Reviewer")
        artifact_id = seed_document(db, "owner")
        _agent_batch(db, artifact_id)
        root = _agent_batch(db, artifact_id, replacement="New")
        busy, first = _feedback_nodes(db, "owner")
        assert db.execute("SELECT 1 FROM plan_node_attempts WHERE node_id=?", (first["id"],)).fetchone() is None
        reply = _agent_batch(db, artifact_id, parent_id=root["id"])
        assert [node["id"] for node in _feedback_nodes(db, "owner")] == [busy["id"], first["id"]]
        assert "left 1 comment and 1 suggestion on" in service.get_node(first["id"], db=db).title

        with write_transaction(db, scope="test_reply_join_settlement") as connection:
            comment_actions.record_decision(
                root["id"], decision="accept", decided_by="Robin", decided_by_thread_id=None, db=connection,
            )
        assert service.get_node(first["id"], db=db).state != "accepted"

        comments.update(reply["id"], {"resolved": True}, db=db)

        assert service.get_node(first["id"], db=db).state == "accepted"
        evidence = db.execute(
            "SELECT payload_json FROM plan_node_events WHERE node_id=? AND event='review_evidence_submitted'",
            (first["id"],),
        ).fetchone()["payload_json"]
        assert f"Comment {root['id']}: suggestion accepted by Robin" in evidence
        assert f"Comment {reply['id']}: resolved" in evidence
    finally:
        db.close()


def test_joined_title_still_counts_an_edit_the_first_batch_carried(isolated_db):
    """Delivery clears the first batch's edit item; the batch's own count still says it held one."""
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author", title="Reviewer")
        artifact_id = seed_document(db, "owner")
        _agent_batch(db, artifact_id)
        root, _ = add_comment(db, artifact_id)
        feedback_batch_edits.record_edit(
            db, artifact_id=artifact_id, recipient_id="owner", author_kind="agent", author_id="author",
            actor_display="Reviewer", path=Path("/workspace/shared/Design.md"), before=b"old\n", after=b"new\n",
        )
        db.commit()
        feedback_batches.close_batch(db, batches(db, state="open")[0]["id"], "quiet")
        _, first = _feedback_nodes(db, "owner")
        assert "edited" in first["title"] and "1 comment" in first["title"]

        _agent_batch(db, artifact_id, parent_id=root["id"])

        assert service.get_node(first["id"], db=db).title == first["title"].replace("1 comment", "2 comments")
    finally:
        db.close()


def test_new_top_level_comment_still_gets_its_own_task(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact_id = seed_document(db, "owner")
        add_person_comment(db, artifact_id)
        add_person_comment(db, artifact_id)

        assert len(_feedback_nodes(db, "owner")) == 2
    finally:
        db.close()
