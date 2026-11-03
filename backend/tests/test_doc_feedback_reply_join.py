"""A reply to a comment joins the open task its thread already has, rather than making a second."""

from __future__ import annotations

from doc_feedback_helpers import add_comment, add_person_comment, seed_document, seed_thread
from doceditor import feedback_batches
from infra.database import get_db
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


def test_person_reply_joins_the_open_task_of_its_thread(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact_id = seed_document(db, "owner")
        root, _ = add_person_comment(db, artifact_id)
        (first,) = _feedback_nodes(db, "owner")

        reply = _person_reply(db, artifact_id, root["id"])

        nodes = _feedback_nodes(db, "owner")
        assert [node["id"] for node in nodes] == [first["id"]]
        joined = service.get_node(first["id"], db=db)
        assert joined.title == first["title"]
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


def test_reply_after_the_task_finished_gets_its_own_task(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact_id = seed_document(db, "owner")
        root, _ = add_person_comment(db, artifact_id)
        (first,) = _feedback_nodes(db, "owner")
        service.transition_node(first["id"], "closed", actor_id=None, provenance="test", db=db)
        db.commit()

        _person_reply(db, artifact_id, root["id"])

        nodes = _feedback_nodes(db, "owner")
        assert len(nodes) == 2 and nodes[1]["id"] != first["id"]
    finally:
        db.close()


def test_reply_to_a_parked_task_gets_its_own_task(isolated_db):
    """A parked task would not see the reply until its own wake, so the reply is not hidden in it."""
    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact_id = seed_document(db, "owner")
        root, _ = add_person_comment(db, artifact_id)
        (first,) = _feedback_nodes(db, "owner")
        holds.pause(
            first["id"], "test", expected_revision=first["revision"], actor_id="owner",
            provenance="test", db=db,
        )
        db.commit()

        _person_reply(db, artifact_id, root["id"])

        assert len(_feedback_nodes(db, "owner")) == 2
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
