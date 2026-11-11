"""A reply to a comment joins the open task its thread already has, rather than making a second."""

from __future__ import annotations

import json
from pathlib import Path

from doc_feedback_helpers import add_comment, add_person_comment, batches, feedback_nodes, seed_document, seed_thread
from doceditor import comment_actions, comments, feedback_batch_edits, feedback_batches, feedback_delivery
from infra.database import get_db, write_transaction
from plans import holds, service
from plans.feedback_nodes import create_feedback_node
from prompts.plan_wakes import INTENT_INPUT_ANSWER_REPLY


def _person_reply(db, artifact_id: str, root_id: str, body: str = "Also the second point.") -> dict:
    row, feedback = add_comment(
        db, artifact_id, author_kind="user", author_id=None, author="Steve", parent_id=root_id,
    )
    db.execute("UPDATE doc_comments SET body=? WHERE id=?", (body, row["id"]))
    db.commit()
    feedback_batches.deliver_person_comment(db, feedback["batch_ids"], artifact_id)
    return row


def _queued_thread(db) -> tuple[str, dict, object]:
    """A document whose owner is busy with one comment's task while a second comment's task waits."""
    seed_thread(db, "owner")
    artifact_id = seed_document(db, "owner")
    add_person_comment(db, artifact_id)
    root, _ = add_person_comment(db, artifact_id)
    busy, first = feedback_nodes(db, "owner")
    selected = "SELECT 1 FROM plan_node_attempts WHERE node_id=?"
    assert db.execute(selected, (busy["id"],)).fetchone() is not None
    assert db.execute(selected, (first["id"],)).fetchone() is None
    return artifact_id, root, first


def test_person_reply_joins_the_open_task_of_its_thread(isolated_db):
    db = get_db()
    try:
        artifact_id, root, first = _queued_thread(db)

        reply = _person_reply(db, artifact_id, root["id"])

        nodes = feedback_nodes(db, "owner")
        assert [node["id"] for node in nodes][1:] == [first["id"]]
        joined = service.get_node(first["id"], db=db)
        assert "1 comment on" in first["title"]
        assert joined.title == first["title"].replace("1 comment on", "2 comments on")
        assert joined.requirements_revision == first["requirements_revision"] + 1
        assert first["intent"].startswith("Answer the person's 1 comment on ")
        assert joined.intent == first["intent"].replace("1 comment on", "2 comments on")
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
        comments = db.execute(
            """SELECT i.comment_id FROM doc_feedback_batch_items i
               JOIN doc_feedback_batches b ON b.id=i.batch_id
               WHERE b.delivered_node_id=? ORDER BY i.comment_id""", (first["id"],),
        ).fetchall()
        assert [row["comment_id"] for row in comments] == sorted([root["id"], reply["id"]])

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


def test_person_reply_joins_the_task_its_owner_is_working_and_wakes_the_owner(isolated_db):
    """The owner may have read the task already, so the reply comes to it as a wake in the same task."""
    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact_id = seed_document(db, "owner")
        root, _ = add_person_comment(db, artifact_id)
        (first,) = feedback_nodes(db, "owner")
        attempts = db.execute("SELECT id FROM plan_node_attempts WHERE node_id=?", (first["id"],)).fetchall()

        reply = _person_reply(db, artifact_id, root["id"], "Also the second point. " + "detail " * 300)

        assert [node["id"] for node in feedback_nodes(db, "owner")] == [first["id"]]
        joined = service.get_node(first["id"], db=db)
        assert joined.description.startswith(first["description"])
        assert f"Comment {reply['id']}" in joined.description
        assert joined.title == first["title"].replace("1 comment on", "2 comments on")
        # A reply to a task already in progress keeps the owner's selection.
        assert joined.requirements_revision == first["requirements_revision"]
        assert db.execute(
            "SELECT id FROM plan_node_attempts WHERE node_id=? AND released_at IS NULL", (first["id"],),
        ).fetchall() == attempts
        (wake,) = db.execute(
            "SELECT id,content,metadata FROM messages WHERE thread_id='owner' AND id LIKE 'plan-wake:intent_input:%'",
        ).fetchall()
        assert wake["id"] == f"plan-wake:intent_input:{first['id']}:doc_comment:{reply['id']}"
        assert json.loads(wake["metadata"])["event_type"] == "plan.intent_input"
        line, payload = wake["content"].split("\n", 1)
        assert line.endswith(INTENT_INPUT_ANSWER_REPLY.format(comment_id=reply["id"]))
        facts = json.loads(payload)["intent_input"]
        assert facts["node_id"] == first["id"] and facts["source_kind"] == "doc_comment"
        assert facts["words"].startswith(f"Comment {reply['id']}") and "Also the second point." in facts["words"]
        from plans.person_input import WORDS_MAX_BYTES
        assert len(facts["words"].encode()) <= WORDS_MAX_BYTES
        # The reply is not queued, so no note says it is.
        assert db.execute(
            "SELECT COUNT(*) FROM context_change_journal WHERE object_id='owner' AND summary LIKE '%is queued%'",
        ).fetchone()[0] == 1
    finally:
        db.close()


def test_tagged_reply_wake_keeps_action_after_bounded_context(isolated_db):
    from doceditor import comment_recipients

    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact_id = seed_document(db, "owner")
        root, _ = add_person_comment(db, artifact_id)
        db.execute("UPDATE doc_comments SET body=?, anchor_text='The quote', author='Earlier author' WHERE id=?",
                   ("prior " * 500, root["id"]))
        db.commit()
        reply, feedback = add_comment(db, artifact_id, author_kind="user", author_id=None,
                                      author="Person", parent_id=root["id"])
        with write_transaction(db, scope="tagged_wake") as connection:
            connection.execute("UPDATE doc_comments SET body='Actionable reply' WHERE id=?", (reply["id"],))
            comment_recipients.save_tags(connection, reply, ["owner"])
        feedback_batches.deliver_person_comment(db, feedback["batch_ids"], artifact_id)
        wake = db.execute("SELECT content FROM messages WHERE thread_id='owner' AND id LIKE 'plan-wake:intent_input:%'").fetchone()
        payload = json.loads(wake[0].split("\n", 1)[1])
        words = payload["intent_input"]["words"]
        context = payload["thread_context"]
        assert 'Highlighted: "The quote"' in context
        assert '- Earlier author:' not in context
        assert '1 earlier comments not shown; read them all with doc_comments_list' in context
        assert 'Earlier author' not in words and 'prior prior' not in words
        assert 'Feedback: Actionable reply' in words
    finally:
        db.close()


def test_multiple_tagged_reply_contexts_name_each_root_when_cut(isolated_db):
    from doceditor import comment_recipients
    from plans.person_input import WORDS_MAX_BYTES

    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact_id = seed_document(db, "owner")
        roots = []
        for _ in range(2):
            root, feedback = add_comment(db, artifact_id, author_kind="user", author_id=None, author="Person")
            db.execute("UPDATE doc_comments SET body=? WHERE id=?", ("prior " * 140, root["id"]))
            db.commit()
            roots.append(root)
        feedback_batches.deliver_person_comment(db, feedback["batch_ids"], artifact_id)
        for root in roots:
            reply, feedback = add_comment(db, artifact_id, author_kind="user", author_id=None,
                                          author="Person", parent_id=root["id"])
            comment_recipients.save_tags(db, reply, ["owner"])
            db.commit()
        feedback_batches.deliver_person_comment(db, feedback["batch_ids"], artifact_id)
        wake = db.execute("SELECT content FROM messages WHERE thread_id='owner' AND id LIKE 'plan-wake:intent_input:%'").fetchone()
        context = json.loads(wake[0].split("\n", 1)[1])["thread_context"]
        assert len(context.encode("utf-8")) <= WORDS_MAX_BYTES
        assert all(f'doc_comments_list(comment_id="{root["id"]}")' in context for root in roots)
    finally:
        db.close()


def test_person_reply_to_a_task_handed_then_released_gets_its_own_task(isolated_db):
    """A task its owner picked up and put down is not being worked, so the reply is not folded into it."""
    from plans import admission

    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact_id = seed_document(db, "owner")
        root, _ = add_person_comment(db, artifact_id)
        (first,) = feedback_nodes(db, "owner")
        admission.release(first["id"], "set_aside", db=db)
        db.commit()

        _person_reply(db, artifact_id, root["id"])

        assert len(feedback_nodes(db, "owner")) == 2
        assert db.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id='owner' AND id LIKE 'plan-wake:intent_input:%'",
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_agent_reply_to_a_task_its_owner_is_working_gets_its_own_task_and_the_next_joins_that(isolated_db):
    """An agent's reply carries no wake, so it never joins a worked task; past one, the next open task takes it."""
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author", title="Reviewer")
        artifact_id = seed_document(db, "owner")
        root = _agent_batch(db, artifact_id)
        _agent_batch(db, artifact_id, parent_id=root["id"])
        first, queued = feedback_nodes(db, "owner")

        later = _agent_batch(db, artifact_id, parent_id=root["id"])

        assert [node["id"] for node in feedback_nodes(db, "owner")] == [first["id"], queued["id"]]
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

        nodes = feedback_nodes(db, "owner")
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

        assert len(feedback_nodes(db, "owner")) == 3
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
        busy, first = feedback_nodes(db, "owner")
        assert db.execute("SELECT 1 FROM plan_node_attempts WHERE node_id=?", (first["id"],)).fetchone() is None
        reply = _agent_batch(db, artifact_id, parent_id=root["id"])
        assert [node["id"] for node in feedback_nodes(db, "owner")] == [busy["id"], first["id"]]
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


def test_reply_batch_made_first_but_delivered_last_still_settles_the_task_it_joined(isolated_db, monkeypatch):
    """The task is found through the batch's delivered task, not through whichever batch is oldest."""
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author", title="Reviewer")
        artifact_id = seed_document(db, "owner")
        root = _agent_batch(db, artifact_id)
        with monkeypatch.context() as patched:
            patched.setattr(feedback_delivery, "recipient_problem", lambda *_: "owner_unavailable")
            early = _agent_batch(db, artifact_id, parent_id=root["id"])
        (late_batch,) = [row for row in batches(db) if row["state"] == "undelivered"]
        later = _agent_batch(db, artifact_id, parent_id=root["id"])
        _, task = feedback_nodes(db, "owner")

        feedback_batches.retry_batch(db, late_batch["id"])

        assert db.execute(
            "SELECT delivered_node_id FROM doc_feedback_batches WHERE id=?", (late_batch["id"],),
        ).fetchone()[0] == task["id"]
        comments.update(early["id"], {"resolved": True}, db=db)
        assert service.get_node(task["id"], db=db).state != "accepted"
        comments.update(later["id"], {"resolved": True}, db=db)
        assert service.get_node(task["id"], db=db).state == "accepted"
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
        _, first = feedback_nodes(db, "owner")
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

        assert len(feedback_nodes(db, "owner")) == 2
    finally:
        db.close()
