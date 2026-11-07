"""Comment routing follows involvement, leave state, and delivered tags."""

import json

import pytest

from doc_feedback_helpers import add_comment, seed_document, seed_thread
from doceditor import comment_actions, comment_recipients, comments, feedback_batches, feedback_comments
from doceditor.comment_create_db import CommentInput, create_in_db
from doceditor.comment_creation import CreateReceipt
from doceditor.comment_state_updates import update_comment
from infra.database import get_db, write_transaction


def test_reply_routing_and_leaving(isolated_db):
    db = get_db()
    for name in ("owner", "first", "second", "outsider"):
        seed_thread(db, name, model=None)
    aid = seed_document(db, "owner")
    root, _ = add_comment(db, aid, author_id="first")
    reply, _ = add_comment(db, aid, author_id="second", parent_id=root["id"])
    assert db.execute(
        "SELECT COUNT(*) FROM context_change_journal WHERE object_kind='artifact' AND object_id=? AND summary LIKE '%commented on%'",
        (aid,),
    ).fetchone()[0] == 2
    with write_transaction(db, scope="duplicate_comment") as connection:
        feedback_batches.record_comment(connection, reply, created=False)
    assert db.execute(
        "SELECT COUNT(*) FROM context_change_journal WHERE object_kind='artifact' AND object_id=? AND summary LIKE '%commented on%'",
        (aid,),
    ).fetchone()[0] == 2
    assert set(feedback_comments.recipients(db, reply)) == {"owner", "first"}
    comment_recipients.leave(db, root, "first")
    db.commit()
    person, _ = add_comment(db, aid, author_kind="user", author_id=None, parent_id=root["id"])
    assert set(feedback_comments.recipients(db, person)) == {"owner", "second"}
    comment_recipients.leave(db, root, "owner")
    db.commit()
    assert "owner" in feedback_comments.recipients(db, person)
    person["tagged_agent_ids"] = ["first"]
    assert "first" in feedback_comments.recipients(db, person)
    db.close()


def test_tag_selection_uses_viewer_roster(isolated_db):
    db = get_db()
    for name in ("viewer", "allowed", "not-an-agent"):
        seed_thread(db, name, model=None)
    db.execute("UPDATE threads SET agent_enabled=1,agent_status='active',agent_responsibility='Test' WHERE id='allowed'")
    comment_recipients.validate_tags(db, user_id="owner-token", thread_id="viewer", agent_ids=["allowed"])
    with pytest.raises(comment_recipients.TagValidationError):
        comment_recipients.validate_tags(db, user_id="owner-token", thread_id="viewer", agent_ids=["not-an-agent"])
    with pytest.raises(comment_recipients.TagValidationError):
        comment_recipients.validate_tags(db, user_id="other-user", thread_id="viewer", agent_ids=["allowed"])
    db.close()


def test_create_fails_closed_when_the_tag_roster_cannot_be_read(isolated_db, monkeypatch):
    import sqlite3

    from doceditor.review_requests import ReviewRosterError

    db = get_db()
    for name in ("owner", "viewer", "allowed"):
        seed_thread(db, name, model=None)
    db.execute("UPDATE threads SET agent_enabled=1,agent_status='active',agent_responsibility='Test' WHERE id='allowed'")
    aid = seed_document(db, "owner")
    root, _ = add_comment(db, aid, author_id="owner")
    comments_before = db.execute("SELECT COUNT(*) FROM doc_comments").fetchone()[0]

    def broken(*args):
        raise sqlite3.OperationalError("private schema detail")

    monkeypatch.setattr(comment_recipients, "agent_rows", broken)
    with pytest.raises(ReviewRosterError):
        create_in_db(CommentInput(
            artifact_id=aid, path=None, body="Hello @Allowed", author="Person", author_kind="user", user_id="owner-token",
            parent_id=root["id"], viewing_thread_id="viewer", tagged_agent_ids=["allowed"],
            tagged_agent_mentions=[{"agent_id": "allowed", "start": 6, "end": 14}],
        ), None, "tagged-roster-down", CreateReceipt())
    assert db.execute("SELECT COUNT(*) FROM doc_comments").fetchone()[0] == comments_before
    assert db.execute("SELECT COUNT(*) FROM doc_comments WHERE id='tagged-roster-down'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM doc_comment_tags").fetchone()[0] == 0
    db.close()


def test_taggable_roster_includes_owner_and_corsia_excludes_viewer_and_unavailable(isolated_db, monkeypatch):
    from doceditor import feedback_delivery

    db = get_db()
    for name in ("viewer", "owner", "corsia", "unavailable"):
        seed_thread(db, name, model=None)
        db.execute("UPDATE threads SET agent_enabled=1,agent_responsibility='Work' WHERE id=?", (name,))
    monkeypatch.setattr(feedback_delivery, "recipient_problem", lambda db, agent: "unavailable" if agent == "unavailable" else None)
    agents = comment_recipients.taggable_agents(db, user_id="owner-token", thread_id="viewer")
    assert {agent["id"] for agent in agents} == {"owner", "corsia"}
    with pytest.raises(comment_recipients.TagValidationError):
        comment_recipients.validate_tags(db, user_id="owner-token", thread_id="viewer", agent_ids=["unavailable"])
    db.close()


def test_top_level_routes_only_to_owner_and_tags(isolated_db):
    db = get_db()
    for name in ("owner", "writer", "tagged", "outsider"):
        seed_thread(db, name, model=None)
    aid = seed_document(db, "owner")
    root, _ = add_comment(db, aid, author_id="writer")
    root["tagged_agent_ids"] = ["tagged"]
    assert set(feedback_comments.recipients(db, root)) == {"owner", "tagged"}
    db.close()


def test_tags_preserve_names_and_hint_counts_notifications(isolated_db):
    db = get_db()
    seed_thread(db, "owner", model=None)
    seed_thread(db, "tagged", title="Original name", model=None)
    aid = seed_document(db, "owner")
    root, _ = add_comment(db, aid, author_kind="user", author_id=None)
    comment_recipients.save_tags(db, root, ["tagged"])
    db.execute("UPDATE threads SET title='New name' WHERE id='tagged'")
    loaded = comments.get_comment(root["id"], db=db)
    assert loaded["tagged_agent_ids"] == ["tagged"]
    assert loaded["tagged_agents"] == [{"id": "tagged", "name": "Original name"}]
    for count in range(4):
        item = {**loaded, "kind": "comment"}
        comment_recipients.prepare_notification(db, [item], "tagged")
        assert ("leave_hint" in item) is (count == 3)
    from plans.feedback_nodes import item_blocks
    assert 'leave=true' in item_blocks({"author_kind": "user"}, [item])
    comment_recipients.reset_action(db, loaded, "tagged")
    item = {**loaded, "kind": "comment"}
    comment_recipients.prepare_notification(db, [item], "tagged")
    assert "leave_hint" not in item
    comment_recipients.leave(db, loaded, "owner")
    comment_recipients.save_tags(db, loaded, ["owner"])
    comment_recipients.prepare_notification(db, [{**loaded, "kind": "comment", "event": "reopened"}], "owner")
    assert db.execute(
        "SELECT left_thread FROM doc_comment_recipient_state WHERE root_id=? AND agent_id='owner'", (root["id"],)
    ).fetchone()[0] == 1
    db.close()


def test_held_tag_joins_only_on_delivery_and_rejoins_after_leave(isolated_db):
    db = get_db()
    for name in ("owner", "tagged", "other", "newcomer"):
        seed_thread(db, name, model=None)
    aid = seed_document(db, "owner")
    root, _ = add_comment(db, aid, author_id="tagged")
    comment_recipients.leave(db, root, "tagged")
    db.commit()
    with write_transaction(db, scope="test_held_tag") as connection:
        tagged_comment, created = comments.create_with_status(
            aid, body="Please join", author="User", author_kind="user", parent_id=root["id"], db=connection,
        )
        comment_recipients.save_tags(connection, tagged_comment, ["tagged", "newcomer"])
        comment_recipients.tags_for_comments(connection, [tagged_comment])
        feedback = feedback_batches.record_comment(
            connection, tagged_comment, created=created, delivery_policy=feedback_batches.DOCUMENT_HELD,
        )
    before, _ = add_comment(db, aid, author_id="other", parent_id=root["id"])
    assert "tagged" not in feedback_comments.recipients(db, before)
    assert "newcomer" not in feedback_comments.recipients(db, before)
    batch_id = next(
        batch_id for batch_id in feedback["batch_ids"]
        if db.execute("SELECT recipient_id FROM doc_feedback_batches WHERE id=?", (batch_id,)).fetchone()[0] == "tagged"
    )
    assert feedback_batches.close_batch(db, batch_id, "send")[0]
    state = db.execute(
        "SELECT left_thread, notifications FROM doc_comment_recipient_state WHERE root_id=? AND agent_id='tagged'",
        (root["id"],),
    ).fetchone()
    assert tuple(state) == (0, 1)
    assert not feedback_batches.close_batch(db, batch_id, "send")[0]
    assert db.execute(
        "SELECT notifications FROM doc_comment_recipient_state WHERE root_id=? AND agent_id='tagged'", (root["id"],),
    ).fetchone()[0] == 1
    after, _ = add_comment(db, aid, author_id="other", parent_id=root["id"])
    assert "tagged" in feedback_comments.recipients(db, after)
    db.close()


@pytest.mark.asyncio
async def test_leave_through_comment_tool(isolated_db, monkeypatch):
    from doceditor import live_events
    from tools.documents import DocCommentUpdateTool

    events = []
    monkeypatch.setattr(live_events, "emit", lambda action, **properties: events.append((action, properties)))
    db = get_db()
    seed_thread(db, "owner", model=None)
    seed_thread(db, "agent", model=None)
    aid = seed_document(db, "owner")
    root, _ = add_comment(db, aid, author_id="agent")
    # Leave never silently drops another change asked for in the same call.
    combined = await DocCommentUpdateTool().execute(comment_id=root["id"], leave=True, resolved=True, _thread_id="agent")
    assert combined.startswith("Error: leave can't be combined with resolved")
    assert events == []
    result = await DocCommentUpdateTool().execute(comment_id=root["id"], leave=True, _thread_id="agent")
    assert json.loads(result) == {"left": True, "comment_id": root["id"]}
    assert events == [("comment_thread_left", {"comment_id": root["id"]})]
    state = db.execute(
        "SELECT left_thread FROM doc_comment_recipient_state WHERE root_id=? AND agent_id='agent'", (root["id"],)
    ).fetchone()
    assert state[0] == 1
    db.close()


def test_leave_suppresses_reply_already_waiting_in_batch(isolated_db):
    db = get_db()
    for name in ("owner", "agent", "other"):
        seed_thread(db, name, model=None)
    aid = seed_document(db, "owner")
    root, _ = add_comment(db, aid, author_id="agent")
    _, feedback = add_comment(db, aid, author_id="other", parent_id=root["id"])
    batch = next(batch_id for batch_id in feedback["batch_ids"] if db.execute(
        "SELECT recipient_id FROM doc_feedback_batches WHERE id=?", (batch_id,),
    ).fetchone()[0] == "agent")
    with write_transaction(db, scope="test_leave_pending") as connection:
        comment_recipients.leave(connection, root, "agent")
    assert feedback_batches.close_batch(db, batch, "send")[1] == ()
    assert tuple(db.execute("SELECT delivery_outcome,item_count FROM doc_feedback_batches WHERE id=?", (batch,)).fetchone()) == ("empty", 0)
    assert not db.execute("SELECT 1 FROM doc_feedback_batch_items WHERE batch_id=?", (batch,)).fetchone()
    db.close()


def test_mixed_held_replies_only_deliver_explicit_tag_to_left_agent(isolated_db):
    db = get_db()
    for name in ("owner", "agent", "other"):
        seed_thread(db, name, model=None)
    aid = seed_document(db, "owner")
    root, _ = add_comment(db, aid, author_id="agent")
    untagged, _ = add_comment(db, aid, author_id="other", parent_id=root["id"])
    tagged, _ = add_comment(db, aid, author_id="other", parent_id=root["id"])
    comment_recipients.save_tags(db, tagged, ["agent"])
    comment_recipients.leave(db, root, "agent")
    items = [{**untagged, "kind": "comment"}, {**tagged, "kind": "comment"}]
    assert [item["id"] for item in comment_recipients.filter_stopped_replies(db, items, "agent", person_owner=False)] == [tagged["id"]]
    assert len(comment_recipients.filter_stopped_replies(db, items, "agent", person_owner=True)) == 2
    db.close()


def test_mixed_left_batch_persists_only_delivered_item_count(isolated_db):
    db = get_db()
    for name in ("owner", "agent", "writer"):
        seed_thread(db, name, model=None)
    aid = seed_document(db, "owner")
    root, _ = add_comment(db, aid, author_id="agent")
    plain, first = add_comment(db, aid, author_id="writer", parent_id=root["id"])
    with write_transaction(db, scope="mixed_left_tag") as connection:
        tagged, created = comments.create_with_status(aid, body="Tagged", author="Writer", author_kind="agent", author_thread_id="writer", parent_id=root["id"], db=connection)
        comment_recipients.save_tags(connection, tagged, ["agent"])
        comment_recipients.tags_for_comments(connection, [tagged])
        feedback_batches.record_comment(connection, tagged, created=created)
        comment_recipients.leave(connection, root, "agent")
    batch = next(batch_id for batch_id in first["batch_ids"] if db.execute(
        "SELECT recipient_id FROM doc_feedback_batches WHERE id=?", (batch_id,)).fetchone()[0] == "agent")
    feedback_batches.close_batch(db, batch, "send")
    assert tuple(db.execute("SELECT state,item_count FROM doc_feedback_batches WHERE id=?", (batch,)).fetchone()) == ("delivered", 1)
    assert [row[0] for row in db.execute("SELECT comment_id FROM doc_feedback_batch_items WHERE batch_id=?", (batch,))] == [tagged["id"]]
    db.close()


def test_failed_node_delivery_rolls_back_rejoin_and_notification(isolated_db, monkeypatch):
    from doceditor import feedback_delivery

    db = get_db()
    for name in ("owner", "agent"):
        seed_thread(db, name, model=None)
    aid = seed_document(db, "owner")
    root, _ = add_comment(db, aid, author_id="agent")
    with write_transaction(db, scope="leave_before_failure") as connection:
        comment_recipients.leave(connection, root, "agent")
        reply, created = comments.create_with_status(
            aid, body="Please join", author="User", author_kind="user", parent_id=root["id"], db=connection,
        )
        comment_recipients.save_tags(connection, reply, ["agent"])
        comment_recipients.tags_for_comments(connection, [reply])
        feedback = feedback_batches.record_comment(connection, reply, created=created, delivery_policy=feedback_batches.DOCUMENT_HELD)
    batch = next(batch_id for batch_id in feedback["batch_ids"] if db.execute(
        "SELECT recipient_id FROM doc_feedback_batches WHERE id=?", (batch_id,),
    ).fetchone()[0] == "agent")
    def fail(*args, **kwargs):
        raise RuntimeError("node creation failed")
    monkeypatch.setattr(feedback_delivery, "create_feedback_node", fail)
    with pytest.raises(RuntimeError, match="node creation failed"):
        feedback_batches.close_batch(db, batch, "send")
    state = db.execute("SELECT left_thread,notifications FROM doc_comment_recipient_state WHERE root_id=? AND agent_id=?", (root["id"], "agent")).fetchone()
    assert tuple(state) == (1, 0)
    assert db.execute("SELECT state FROM doc_feedback_batches WHERE id=?", (batch,)).fetchone()[0] == "open"
    db.close()


def test_local_only_reply_to_other_agent_author_is_held(isolated_db):
    db = get_db()
    for name in ("owner", "cloud", "local"):
        seed_thread(db, name, model=None)
    db.execute("UPDATE threads SET local_only=1 WHERE id='local'")
    db.commit()
    aid = seed_document(db, "owner")
    root, _ = add_comment(db, aid, author_kind="user", author_id=None)
    add_comment(db, aid, author_id="cloud", parent_id=root["id"])
    with write_transaction(db, scope="local_reply") as connection:
        reply, created = comments.create_with_status(
            aid, body="Reply", author="Local", author_kind="agent",
            author_thread_id="local", parent_id=root["id"], db=connection,
        )
        result = feedback_batches.record_comment(connection, reply, created=created)
    # A participant reached through the thread is refused, and so is the thread's own owner.
    assert any("cloud is cloud-bound or has no model selected" in refusal
               for refusal in result.get("tag_refusals", []))
    assert any("owner is cloud-bound or has no model selected" in refusal
               for refusal in result.get("tag_refusals", []))
    assert not batched_recipients(db, reply["id"]) & {"cloud", "owner"}
    db.close()


def batched_recipients(db, comment_id):
    """The recipients a comment actually has batch rows for."""
    return {str(row[0]) for row in db.execute(
        "SELECT b.recipient_id FROM doc_feedback_batch_items i JOIN doc_feedback_batches b ON b.id=i.batch_id "
        "WHERE i.comment_id=?", (comment_id,),
    ).fetchall()}


def test_local_only_reply_to_the_thread_owner_is_refused(isolated_db):
    """The thread's owner is a recipient like any other: an unbound owner gets no Local only reply."""
    db = get_db()
    for name in ("owner", "local"):
        seed_thread(db, name, model=None)
    db.execute("UPDATE threads SET local_only=1 WHERE id='local'")
    db.commit()
    aid = seed_document(db, "owner")
    reply, result = add_comment(db, aid, author_id="local")
    assert any("owner is cloud-bound or has no model selected" in refusal
               for refusal in result.get("tag_refusals", []))
    assert batched_recipients(db, reply["id"]) == set()
    db.close()


def test_local_only_reply_to_the_root_comment_author_is_refused(isolated_db, monkeypatch):
    """The root comment's agent author is a recipient like any other; a local owner is still delivered."""
    from infra import local_only

    monkeypatch.setattr(local_only, "_is_cloud", lambda selector, **_kwargs: selector == "cloud")
    db = get_db()
    seed_thread(db, "owner", model="local")
    seed_thread(db, "root-author", model="cloud")
    seed_thread(db, "local", model="local")
    db.execute("UPDATE threads SET local_only=1 WHERE id='local'")
    db.commit()
    aid = seed_document(db, "owner")
    root, _ = add_comment(db, aid, author_id="root-author")
    reply, result = add_comment(db, aid, author_id="local", parent_id=root["id"])
    assert any("root-author is cloud-bound or has no model selected" in refusal
               for refusal in result.get("tag_refusals", []))
    assert batched_recipients(db, reply["id"]) == {"owner"}
    db.close()


def test_reply_from_an_agent_that_is_not_local_only_reaches_a_cloud_owner(isolated_db, monkeypatch):
    """Only a Local only sender is held: an ordinary agent's reply reaches the same cloud owner."""
    from infra import local_only

    monkeypatch.setattr(local_only, "_is_cloud", lambda selector, **_kwargs: selector == "cloud")
    db = get_db()
    seed_thread(db, "owner", model="cloud")
    seed_thread(db, "writer", model="local")
    aid = seed_document(db, "owner")
    reply, result = add_comment(db, aid, author_id="writer")
    assert "tag_refusals" not in result
    assert batched_recipients(db, reply["id"]) == {"owner"}
    db.close()


def assert_refused_batch_settled(db, batch, comment_id):
    """A refusal that can't clear on retry settles the batch empty instead of leaving it retryable."""
    state = db.execute("SELECT state,delivery_outcome,undelivered_reason FROM doc_feedback_batches WHERE id=?",
                       (batch,)).fetchone()
    assert tuple(state) == ("delivered", "empty", None)
    assert not db.execute("SELECT 1 FROM doc_feedback_batch_items WHERE batch_id=? AND comment_id=?",
                          (batch, comment_id)).fetchone()


def test_pending_reply_to_other_agent_rechecks_locality(isolated_db):
    db = get_db()
    for name in ("owner", "cloud", "local"):
        seed_thread(db, name, model=None)
    aid = seed_document(db, "owner")
    root, _ = add_comment(db, aid, author_kind="user", author_id=None)
    add_comment(db, aid, author_id="cloud", parent_id=root["id"])
    reply, result = add_comment(db, aid, author_id="local", parent_id=root["id"])
    batch = next(batch_id for batch_id in result["batch_ids"] if db.execute(
        "SELECT recipient_id FROM doc_feedback_batches WHERE id=?", (batch_id,),
    ).fetchone()[0] == "cloud")
    db.execute("UPDATE threads SET local_only=1 WHERE id='local'")
    db.commit()
    feedback_batches.close_batch(db, batch, "send")
    assert_refused_batch_settled(db, batch, reply["id"])
    db.close()


def test_pending_reply_from_deleted_agent_is_not_delivered(isolated_db):
    db = get_db()
    for name in ("owner", "cloud", "local"):
        seed_thread(db, name, model=None)
    aid = seed_document(db, "owner")
    root, _ = add_comment(db, aid, author_kind="user", author_id=None)
    add_comment(db, aid, author_id="cloud", parent_id=root["id"])
    reply, result = add_comment(db, aid, author_id="local", parent_id=root["id"])
    batch = next(batch_id for batch_id in result["batch_ids"] if db.execute(
        "SELECT recipient_id FROM doc_feedback_batches WHERE id=?", (batch_id,),
    ).fetchone()[0] == "cloud")
    db.execute("DELETE FROM threads WHERE id='local'")
    db.commit()
    feedback_batches.close_batch(db, batch, "send")
    assert_refused_batch_settled(db, batch, reply["id"])
    db.close()


def test_agent_local_only_tag_is_refused_but_person_tag_is_not(isolated_db, monkeypatch):
    from doceditor import live_events
    from infra import local_only

    events = []
    monkeypatch.setattr(live_events, "emit", lambda action, **properties: events.append((action, properties)))
    db = get_db()
    for name in ("owner", "writer", "recipient"):
        seed_thread(db, name, model=None)
    aid = seed_document(db, "owner")
    monkeypatch.setattr(local_only, "send_needs_hold", lambda sender, recipient, db, **kwargs: recipient["id"] == "recipient")
    with write_transaction(db, scope="test_local_tag") as connection:
        row, created = comments.create_with_status(aid, body="Tag", author="Writer", author_kind="agent",
                                                    author_thread_id="writer", db=connection)
        comment_recipients.save_tags(connection, row, ["recipient"])
        comment_recipients.tags_for_comments(connection, [row])
        result = feedback_batches.record_comment(connection, row, created=created)
    assert len(result["tag_refusals"]) == 1
    assert row["id"] in result["tag_refusals"][0] and "recipient is cloud-bound" in result["tag_refusals"][0]
    assert db.execute("SELECT COUNT(*) FROM doc_feedback_batches WHERE recipient_id='recipient'").fetchone()[0] == 0
    assert events == [("comment_local_only_refused", {"comment_id": row["id"], "reply": False})]
    with write_transaction(db, scope="test_local_tag_replay") as connection:
        replay = feedback_batches.record_comment(connection, row, created=False)
    assert replay["tag_refusals"] == result["tag_refusals"]
    assert len(events) == 1
    with write_transaction(db, scope="test_person_tag") as connection:
        row, created = comments.create_with_status(aid, body="Person", author="User", author_kind="user", db=connection)
        comment_recipients.save_tags(connection, row, ["recipient"])
        comment_recipients.tags_for_comments(connection, [row])
        result = feedback_batches.record_comment(connection, row, created=created)
    assert "tag_refusals" not in result
    assert db.execute("SELECT COUNT(*) FROM doc_feedback_batches WHERE recipient_id='recipient'").fetchone()[0] == 1
    assert len(events) == 1
    db.close()


def test_pending_agent_tag_rechecks_locality_before_delivery(isolated_db, monkeypatch):
    from infra import local_only

    db = get_db()
    for name in ("owner", "writer", "recipient"):
        seed_thread(db, name, model=None)
    aid = seed_document(db, "owner")
    monkeypatch.setattr(local_only, "send_needs_hold", lambda sender, recipient, db, **kwargs: False)
    with write_transaction(db, scope="test_pending_tag") as connection:
        row, created = comments.create_with_status(aid, body="Tag", author="Writer", author_kind="agent",
                                                    author_thread_id="writer", db=connection)
        comment_recipients.save_tags(connection, row, ["recipient"])
        comment_recipients.tags_for_comments(connection, [row])
        result = feedback_batches.record_comment(connection, row, created=created)
    batch = next(batch_id for batch_id in result["batch_ids"] if db.execute(
        "SELECT recipient_id FROM doc_feedback_batches WHERE id=?", (batch_id,)).fetchone()[0] == "recipient")
    monkeypatch.setattr(local_only, "send_needs_hold", lambda sender, recipient, db, **kwargs: True)
    feedback_batches.close_batch(db, batch, "send")
    assert_refused_batch_settled(db, batch, row["id"])
    db.close()


def test_pending_tag_names_recipient_when_admission_fails(isolated_db, monkeypatch):
    from doceditor import feedback_delivery

    db = get_db()
    for name in ("owner", "writer", "recipient"):
        seed_thread(db, name, model=None)
    aid = seed_document(db, "owner")
    with write_transaction(db, scope="test_pending_unavailable") as connection:
        row, created = comments.create_with_status(aid, body="Tag", author="Writer", author_kind="agent",
                                                    author_thread_id="writer", db=connection)
        comment_recipients.save_tags(connection, row, ["recipient"])
        comment_recipients.tags_for_comments(connection, [row])
        result = feedback_batches.record_comment(connection, row, created=created)
    batch = next(batch_id for batch_id in result["batch_ids"] if db.execute(
        "SELECT recipient_id FROM doc_feedback_batches WHERE id=?", (batch_id,)).fetchone()[0] == "recipient")
    monkeypatch.setattr(feedback_delivery, "recipient_problem", lambda db, recipient: "owner_archived")
    feedback_batches.close_batch(db, batch, "send")
    state = db.execute("SELECT state,undelivered_reason FROM doc_feedback_batches WHERE id=?", (batch,)).fetchone()
    assert tuple(state) == ("undelivered", "owner_archived")
    note = db.execute("SELECT summary FROM context_change_journal WHERE object_id='writer' AND summary LIKE '%doc_feedback_send%' ORDER BY rowid DESC LIMIT 1").fetchone()
    assert note and aid in note[0] and "owner archived" in note[0]
    db.close()


def test_mixed_batch_admission_failure_keeps_standard_retry_note(isolated_db, monkeypatch):
    from doceditor import feedback_delivery

    db = get_db()
    for name in ("owner", "writer"):
        seed_thread(db, name, model=None)
    aid = seed_document(db, "owner")
    with write_transaction(db, scope="mixed_admission") as connection:
        plain, created = comments.create_with_status(aid, body="Ordinary", author="Writer", author_kind="agent", author_thread_id="writer", db=connection)
        result = feedback_batches.record_comment(connection, plain, created=created)
        tagged, created = comments.create_with_status(aid, body="Tagged", author="Writer", author_kind="agent", author_thread_id="writer", db=connection)
        comment_recipients.save_tags(connection, tagged, ["owner"])
        comment_recipients.tags_for_comments(connection, [tagged])
        feedback_batches.record_comment(connection, tagged, created=created)
    batch = result["batch_ids"][0]
    monkeypatch.setattr(feedback_delivery, "recipient_problem", lambda db, recipient: "owner_archived")
    feedback_batches.close_batch(db, batch, "send")
    note = db.execute("SELECT summary FROM context_change_journal WHERE object_id='writer' AND summary LIKE '%doc_feedback_send%' ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    assert "2 comments" in note and "owner archived" in note and aid in note
    assert db.execute("SELECT COUNT(*) FROM doc_feedback_batch_items WHERE batch_id=?", (batch,)).fetchone()[0] == 2
    db.close()


@pytest.mark.parametrize("existing_role", ["owner", "participant"])
def test_tagged_reply_to_an_existing_recipient_keeps_its_tag_effects(isolated_db, monkeypatch, existing_role):
    """A delivered tag is still a tag: the recipient is brought back and given the thread context."""
    from doceditor import feedback_delivery

    db = get_db()
    for name in ("owner", "writer", "recipient"):
        seed_thread(db, name, model=None)
    recipient = "owner" if existing_role == "owner" else "recipient"
    aid = seed_document(db, "owner")
    root, _ = add_comment(db, aid, author_id=recipient)
    with write_transaction(db, scope="ordinary_recipient") as connection:
        reply, created = comments.create_with_status(aid, body="Reply", author="Writer", author_kind="agent",
                                                     author_thread_id="writer", parent_id=root["id"], db=connection)
        comment_recipients.save_tags(connection, reply, [recipient])
        comment_recipients.tags_for_comments(connection, [reply])
        result = feedback_batches.record_comment(connection, reply, created=created)
    assert "tag_refusals" not in result
    batch = next(batch_id for batch_id in result["batch_ids"] if db.execute(
        "SELECT recipient_id FROM doc_feedback_batches WHERE id=?", (batch_id,)).fetchone()[0] == recipient)
    seen = []
    original = feedback_delivery.create_feedback_node
    def capture(db, recipient_id, batch, *, items):
        seen.extend(items)
        return original(db, recipient_id, batch, items=items)
    monkeypatch.setattr(feedback_delivery, "create_feedback_node", capture)
    feedback_batches.close_batch(db, batch, "send")
    assert db.execute("SELECT state FROM doc_feedback_batches WHERE id=?", (batch,)).fetchone()[0] == "delivered"
    assert "thread_context" in seen[0]
    assert db.execute("SELECT left_thread FROM doc_comment_recipient_state WHERE root_id=? AND agent_id=?",
                      (root["id"], recipient)).fetchone()[0] == 0
    db.close()


def test_tag_locality_does_not_emit_held_event(isolated_db, monkeypatch):
    from doceditor import live_events
    from infra import local_only

    db = get_db()
    seed_thread(db, "writer", model=None)
    seed_thread(db, "recipient", model=None)
    seed_thread(db, "owner", model=None)
    aid = seed_document(db, "owner")
    comment, _ = add_comment(db, aid, author_id="writer")
    db.execute("UPDATE threads SET local_only=1 WHERE id='writer'")
    db.commit()
    events = []
    monkeypatch.setattr(local_only, "_emit", lambda *args: events.append(args))
    documents = []
    monkeypatch.setattr(live_events, "emit", lambda action, **properties: documents.append((action, properties)))
    sender = db.execute("SELECT id,local_only,project_id FROM threads WHERE id='writer'").fetchone()
    refusal = comment_recipients.locality_disposition(db, comment, "recipient", sender)
    assert refusal
    assert events == []
    # The comment is refused, not held for approval, so it reports its own documents event instead.
    assert documents == [("comment_local_only_refused", {"comment_id": comment["id"], "reply": False})]
    assert local_only.send_needs_hold({"id": "writer", "local_only": 1}, {"id": "recipient", "model": None}, db=db)
    assert events[0][0] == "local_only.send_held"
    db.close()


def test_a_local_only_pending_batch_is_refused_as_a_whole_and_reported_once(isolated_db, monkeypatch):
    """Nothing in a held batch slips through: a reply, a tagged reply and a tagged root stop together."""
    from doceditor import feedback_delivery
    from infra import local_only

    db = get_db()
    for name in ("owner", "writer", "recipient"):
        seed_thread(db, name, model=None)
    aid = seed_document(db, "owner")
    root, _ = add_comment(db, aid, author_id="recipient")
    monkeypatch.setattr(local_only, "send_needs_hold", lambda *args, **kwargs: False)
    with write_transaction(db, scope="mixed_pending") as connection:
        plain, created = comments.create_with_status(aid, body="Ordinary", author="Writer", author_kind="agent", author_thread_id="writer", parent_id=root["id"], db=connection)
        result = feedback_batches.record_comment(connection, plain, created=created)
        ordinary_tagged, created = comments.create_with_status(aid, body="Ordinary tagged", author="Writer", author_kind="agent", author_thread_id="writer", parent_id=root["id"], db=connection)
        comment_recipients.save_tags(connection, ordinary_tagged, ["recipient"])
        comment_recipients.tags_for_comments(connection, [ordinary_tagged])
        feedback_batches.record_comment(connection, ordinary_tagged, created=created)
        tagged, created = comments.create_with_status(aid, body="Tagged", author="Writer", author_kind="agent", author_thread_id="writer", db=connection)
        comment_recipients.save_tags(connection, tagged, ["recipient"])
        comment_recipients.tags_for_comments(connection, [tagged])
        feedback_batches.record_comment(connection, tagged, created=created)
    batch = next(batch_id for batch_id in result["batch_ids"] if db.execute(
        "SELECT recipient_id FROM doc_feedback_batches WHERE id=?", (batch_id,)).fetchone()[0] == "recipient")
    notes = []
    finals = []
    def note(db, batch, reason, **kwargs):
        notes.append(reason)
        finals.append(kwargs.get("final"))
    monkeypatch.setattr(feedback_delivery, "undelivered_note", note)
    monkeypatch.setattr(local_only, "send_needs_hold", lambda *args, **kwargs: True)
    feedback_batches.close_batch(db, batch, "send")
    assert len(notes) == 1 and "recipient is cloud-bound" in notes[0]
    # The refusal is sent whole: a retry can't clear Local only, so it never gets the retry wording.
    assert finals == [True]
    assert all(comment_id in notes[0] for comment_id in (plain["id"], ordinary_tagged["id"], tagged["id"]))
    assert_refused_batch_settled(db, batch, tagged["id"])
    assert not db.execute("SELECT 1 FROM doc_feedback_batch_items WHERE batch_id=?", (batch,)).fetchone()
    assert comments.get_comment(tagged["id"], db=db)["body"] == "Tagged"
    db.close()


def test_tagged_reply_context_is_bounded_and_not_an_item(isolated_db):
    from plans.feedback_items import feedback_item_counts
    from plans.feedback_nodes import item_blocks
    from plans.person_input import WORDS_MAX_BYTES

    db = get_db()
    for name in ("owner", "first", "second", "recipient"):
        seed_thread(db, name, model=None)
    aid = seed_document(db, "owner")
    root, _ = add_comment(db, aid, author_id="first")
    db.execute("UPDATE doc_comments SET author='First', body='Root text', anchor_text='Highlighted passage' WHERE id=?", (root["id"],))
    db.commit()
    prior, _ = add_comment(db, aid, author_id="second", parent_id=root["id"])
    db.execute("UPDATE doc_comments SET author='Second', body='Earlier text' WHERE id=?", (prior["id"],))
    db.commit()
    reply, _ = add_comment(db, aid, author_id="first", parent_id=root["id"])
    comment_recipients.save_tags(db, reply, ["recipient"])
    item = {**reply, "kind": "comment", "body": "Actionable text"}
    comment_recipients.prepare_notification(db, [item], "recipient")
    rendered = item_blocks({"author_kind": "agent"}, [item])
    assert 'Highlighted: "Highlighted passage"' in rendered
    assert rendered.index('- First: Root text') < rendered.index('- Second: Earlier text') < rendered.index('Feedback: Actionable text')
    assert 'earlier comments not shown' not in rendered
    assert feedback_item_counts([item]) == (0, 1, 0)
    db.execute("UPDATE doc_comments SET body=? WHERE id=?", ("é" * 2000, prior["id"]))
    item.pop("thread_context")
    comment_recipients.prepare_notification(db, [item], "recipient")
    assert len(item["thread_context"].encode()) <= WORDS_MAX_BYTES
    assert '2 earlier comments not shown; read them all with doc_comments_list' in item["thread_context"]
    assert '- Second:' not in item["thread_context"]
    assert 'Feedback: Actionable text' in item_blocks({"author_kind": "agent"}, [item])
    db.close()


def test_thread_context_withholds_local_only_authors_from_a_cloud_recipient(isolated_db):
    db = get_db()
    for name in ("owner", "local", "cloud", "recipient"):
        seed_thread(db, name, model=None)
    db.execute("UPDATE threads SET local_only=1 WHERE id='local'")
    aid = seed_document(db, "owner")
    root, _ = add_comment(db, aid, author_kind="user", author_id=None)
    private, _ = add_comment(db, aid, author_id="local", parent_id=root["id"])
    shared, _ = add_comment(db, aid, author_id="cloud", parent_id=root["id"])
    db.execute("UPDATE doc_comments SET author='Local', body='Private text' WHERE id=?", (private["id"],))
    db.execute("UPDATE doc_comments SET author='Cloud', body='Shared text' WHERE id=?", (shared["id"],))
    db.commit()
    reply, _ = add_comment(db, aid, author_kind="user", author_id=None, parent_id=root["id"])
    comment_recipients.save_tags(db, reply, ["recipient"])
    item = {**reply, "kind": "comment"}
    comment_recipients.prepare_notification(db, [item], "recipient")
    assert "Private text" not in item["thread_context"]
    assert "- Local: (not shown: this agent is Local only)" in item["thread_context"]
    assert "- Cloud: Shared text" in item["thread_context"]
    db.execute("UPDATE threads SET local_only=0 WHERE id='local'")
    assert "- Local: Private text" in comment_recipients.thread_context(db, reply, "recipient")
    db.close()


def test_thread_context_keeps_newest_whole_comments_and_counts_omissions(isolated_db):
    db = get_db()
    for name in ("owner", "first", "second"):
        seed_thread(db, name, model=None)
    aid = seed_document(db, "owner")
    root, _ = add_comment(db, aid, author_id="first")
    older, _ = add_comment(db, aid, author_id="second", parent_id=root["id"])
    newer, _ = add_comment(db, aid, author_id="first", parent_id=root["id"])
    reply, _ = add_comment(db, aid, author_id="second", parent_id=root["id"])
    db.execute("UPDATE doc_comments SET body=? WHERE id=?", ("old " * 500, older["id"]))
    db.execute("UPDATE doc_comments SET body='Newest full comment',author='Newest' WHERE id=?", (newer["id"],))
    context = comment_recipients.thread_context(db, reply, "owner")
    assert '2 earlier comments not shown; read them all with doc_comments_list' in context
    assert '- Newest: Newest full comment' in context
    assert 'old old' not in context
    db.close()


def test_agent_actions_reset_the_notification_count(isolated_db):
    db = get_db()
    seed_thread(db, "owner", model=None)
    seed_thread(db, "agent", model=None)
    aid = seed_document(db, "owner")
    root, _ = add_comment(db, aid, author_id="agent", replacement="new text")

    def set_count():
        for _ in range(3):
            comment_recipients.prepare_notification(db, [{**root, "kind": "comment"}], "agent")
        db.commit()

    def count():
        return db.execute(
            "SELECT notifications FROM doc_comment_recipient_state WHERE root_id=? AND agent_id='agent'", (root["id"],)
        ).fetchone()[0]

    set_count()
    create_in_db(CommentInput(
        artifact_id=aid, path=None, body="Reply", author="agent", author_kind="agent", user_id="owner-token",
        author_thread_id="agent", parent_id=root["id"],
    ), None, "reply-reset", CreateReceipt())
    assert count() == 0
    set_count()
    with write_transaction(db, scope="test_resolve") as connection:
        update_comment(connection, root["id"], {"resolved": True}, actor_thread_id="agent")
    assert count() == 0
    set_count()
    with write_transaction(db, scope="test_decide") as connection:
        comment_actions._record_decision_row(
            connection, root["id"], decision="reject", decided_by="agent", decided_by_thread_id="agent",
            decision_event_id="decision", decided_at="2026-01-01T00:00:00Z",
        )
    assert count() == 0
    set_count()
    with write_transaction(db, scope="test_leave") as connection:
        comment_recipients.leave(connection, root, "agent")
    assert count() == 0
    db.close()
