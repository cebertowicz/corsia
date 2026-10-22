"""DCOL-FR4.3/4.4/4.8 feedback-node delivery behavior."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from doc_feedback_helpers import add_comment, batches, seed_document, seed_thread
from doceditor import feedback_batches
from infra.database import get_db
from plans import admission, service
from plans.feedback_nodes import AGENT_FRAMING, PROVENANCE, USER_FRAMING, create_feedback_node


def _feedback_nodes(db) -> list[dict]:
    return [
        dict(row) for row in db.execute(
            """SELECT n.* FROM plan_nodes n
               JOIN plan_node_events e ON e.node_id=n.id
               WHERE e.provenance=? AND e.event='node_created'
               ORDER BY n.created_at,n.id""",
            (PROVENANCE,),
        ).fetchall()
    ]


def test_idle_owner_is_selected_but_busy_owner_is_not_preempted(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "busy-owner")
        seed_thread(db, "idle-owner")
        seed_thread(db, "author", title="Reviewer")

        busy_plan = service.ensure_plan("thread", "busy-owner", db=db)
        current = service.add_node(
            busy_plan.id, kind="task", title="Current work",
            acceptance_criteria="Current work is complete.", db=db,
        )
        service.transition_node(current.id, "active", db=db)
        admission.reconcile_plan(
            busy_plan.id, actor_id=None, provenance="test", db=db,
        )
        assert db.execute(
            "SELECT 1 FROM plan_node_attempts WHERE node_id=? AND released_at IS NULL",
            (current.id,),
        ).fetchone()

        busy_doc = seed_document(db, "busy-owner", name="busy.md")
        add_comment(db, busy_doc)
        feedback_batches.close_batch(db, batches(db, state="open")[0]["id"], "quiet")
        busy_feedback = _feedback_nodes(db)[0]
        assert db.execute(
            "SELECT 1 FROM plan_node_attempts WHERE node_id=? AND released_at IS NULL",
            (busy_feedback["id"],),
        ).fetchone() is None

        idle_doc = seed_document(db, "idle-owner", name="idle.md")
        add_comment(db, idle_doc)
        idle_batch = [row for row in batches(db, state="open") if row["artifact_id"] == idle_doc][0]
        feedback_batches.close_batch(db, idle_batch["id"], "quiet")
        idle_feedback = _feedback_nodes(db)[1]
        attempt = db.execute(
            "SELECT executor_thread_id FROM plan_node_attempts WHERE node_id=? AND released_at IS NULL",
            (idle_feedback["id"],),
        ).fetchone()
        assert attempt["executor_thread_id"] == "idle-owner"
    finally:
        db.close()


def test_node_link_provenance_framing_counts_and_idempotency(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author", title="Product Experience")
        artifact_id = seed_document(db, "owner", name="DRAFT-PRD.md")
        add_comment(db, artifact_id)
        add_comment(db, artifact_id, replacement="replacement text")
        batch = batches(db, state="open")[0]
        feedback_batches.close_batch(db, batch["id"], "quiet")
        delivered = batches(db)[0]
        node = service.get_node(delivered["delivered_node_id"], db=db)

        assert node.title == "Product Experi… left 1 comment and 1 suggestion on DRAFT-PRD"
        assert len(node.title) == service.MAX_NODE_TITLE
        assert node.description.startswith(AGENT_FRAMING)
        assert "Comment " in node.description and "Suggested replacement: replacement text" in node.description
        assert "workspace" not in node.description.lower()
        link = db.execute(
            "SELECT * FROM plan_node_artifact_links WHERE node_id=?",
            (node.id,),
        ).fetchone()
        assert (link["artifact_id"], link["role"], link["provenance"], link["source_event_id"]) == (
            artifact_id, "input", PROVENANCE, batch["id"],
        )
        again = create_feedback_node(db, "owner", delivered)
        assert (again.node_id, again.created) == (node.id, False)
        assert len(_feedback_nodes(db)) == 1
    finally:
        db.close()


def test_user_batch_uses_fixed_user_framing_and_ui_recipient_creates_no_node(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner", title="Owner")
        artifact_id = seed_document(db, "owner")
        add_comment(db, artifact_id, author_kind="user", author_id=None, author="Steve")
        user_batch = batches(db, state="delivered")[0]
        user_node = service.get_node(user_batch["delivered_node_id"], db=db)
        assert user_node.description.startswith(USER_FRAMING)
        # A reply under a comment the agent then resolves is hidden from the user.
        assert "Leave the user's comment open for them to resolve" in user_node.description

        root, _ = add_comment(
            db, artifact_id, author_kind="user", author_id=None, author="Steve",
        )
        add_comment(db, artifact_id, author_id="owner", author="Owner", parent_id=root["id"])
        ui_batch = [
            row for row in batches(db, state="open")
            if row["author_id"] == "owner" and row["recipient_id"] == "ui"
        ][0]
        feedback_batches.close_batch(db, ui_batch["id"], "quiet")
        ui_batch = [row for row in batches(db) if row["id"] == ui_batch["id"]][0]
        assert ui_batch["state"] == "delivered"
        assert ui_batch["delivered_node_id"] is None
    finally:
        db.close()


def test_undeliverable_reasons_repeat_once_then_retry_after_fix(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner", model="missing/model")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner", name="undelivered.md")
        add_comment(db, artifact_id)
        batch = batches(db, state="open")[0]
        feedback_batches.close_batch(db, batch["id"], "quiet")
        failed = batches(db)[0]
        assert (failed["state"], failed["undelivered_reason"], failed["attempts"]) == (
            "undelivered", "owner_unaddressable", 1,
        )
        notes = db.execute(
            "SELECT summary FROM context_change_journal WHERE object_id='author' ORDER BY seq"
        ).fetchall()
        assert notes[-1]["summary"] == (
            "1 comment on /workspace/shared/undelivered.md not delivered: "
            f'owner unaddressable. Retry with doc_feedback_send(artifact_id="{artifact_id}") '
            "once the owner can receive it."
        )

        _, first_result = add_comment(db, artifact_id)
        _, second_result = add_comment(db, artifact_id)
        assert [row["id"] for row in first_result["undelivered_batches"]] == [batch["id"]]
        assert second_result["undelivered_batches"] == []

        db.execute("UPDATE threads SET model=? WHERE id='owner'", ("openai/test-checkpoint",))
        db.commit()
        assert feedback_batches.retry_batch(db, batch["id"])[0]
        delivered = [row for row in batches(db) if row["id"] == batch["id"]][0]
        assert (delivered["state"], delivered["attempts"], delivered["undelivered_reason"]) == (
            "delivered", 2, None,
        )
        assert feedback_batches.retry_batch(db, batch["id"])[0] is False
        assert len([node for node in _feedback_nodes(db) if node["id"] == delivered["delivered_node_id"]]) == 1
    finally:
        db.close()


def test_submit_failure_is_immediate_and_repeated_on_the_next_create_once(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner", model="missing/model")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner")

        _, submitted = add_comment(db, artifact_id, submit=True)
        batch_id = submitted["batch_ids"][0]
        assert [row["id"] for row in submitted["undelivered_batches"]] == [batch_id]

        _, repeated = add_comment(db, artifact_id)
        _, consumed = add_comment(db, artifact_id)
        assert [row["id"] for row in repeated["undelivered_batches"]] == [batch_id]
        assert consumed["undelivered_batches"] == []
    finally:
        db.close()


def test_no_owner_and_archived_owner_are_visible_undelivered_states(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        first_doc = seed_document(db, "owner", name="no-owner.md")
        second_doc = seed_document(db, "owner", name="archived.md")
        add_comment(db, first_doc)
        no_owner = [row for row in batches(db, state="open") if row["artifact_id"] == first_doc][0]
        db.execute("UPDATE doc_feedback_batches SET recipient_id='' WHERE id=?", (no_owner["id"],))
        db.commit()
        feedback_batches.close_batch(db, no_owner["id"], "quiet")

        add_comment(db, second_doc)
        archived = [row for row in batches(db, state="open") if row["artifact_id"] == second_doc][0]
        db.execute(
            "UPDATE threads SET lifecycle_state='archived',archived_reason='user',archived_at='t' "
            "WHERE id='owner'"
        )
        db.commit()
        feedback_batches.close_batch(db, archived["id"], "quiet")
        reasons = {row["artifact_id"]: row["undelivered_reason"] for row in batches(db)}
        assert reasons[first_doc] == "no_owner"
        assert reasons[second_doc] == "owner_archived"
    finally:
        db.close()


def test_restart_sweep_and_two_racing_sources_deliver_each_batch_once(isolated_db):
    db = get_db()
    seed_thread(db, "owner")
    seed_thread(db, "author")
    restart_doc = seed_document(db, "owner", name="restart.md")
    race_doc = seed_document(db, "owner", name="race.md")
    add_comment(db, restart_doc)
    add_comment(db, race_doc)
    due = {row["artifact_id"]: row for row in batches(db, state="open")}
    db.execute(
        "UPDATE doc_feedback_batches SET closes_at='2999-01-01T00:00:00+00:00' WHERE id=?",
        (due[race_doc]["id"],),
    )
    db.commit()
    db.close()

    restarted = get_db()
    try:
        feedback_batches.close_due(due[restart_doc]["closes_at"], db=restarted)
    finally:
        restarted.close()

    barrier = threading.Barrier(2)

    def race_close() -> bool:
        connection = get_db()
        try:
            barrier.wait()
            return feedback_batches.close_batch(connection, due[race_doc]["id"], "quiet")[0]
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: race_close(), range(2)))
    assert sorted(outcomes) == [False, True]

    check = get_db()
    try:
        assert all(row["state"] == "delivered" for row in batches(check))
        assert len(_feedback_nodes(check)) == 2
        assert check.execute(
            "SELECT COUNT(*) FROM plan_node_artifact_links WHERE provenance=?",
            (PROVENANCE,),
        ).fetchone()[0] == 2
    finally:
        check.close()


def test_existing_unavailable_catalog_row_stays_undelivered_without_a_plan_node(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        db.execute("UPDATE models SET available=0 WHERE id='openai/test-checkpoint'")
        db.commit()

        artifact_id = seed_document(db, "owner")
        add_comment(db, artifact_id)
        batch = batches(db, state="open")[0]
        feedback_batches.close_batch(db, batch["id"], "quiet")

        failed = batches(db)[0]
        assert (failed["state"], failed["undelivered_reason"], failed["delivered_node_id"]) == (
            "undelivered", "owner_unaddressable", None,
        )
        assert _feedback_nodes(db) == []
    finally:
        db.close()


def test_invalid_target_config_refuses_one_due_batch_without_stopping_the_sweep(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "bad-owner")
        seed_thread(db, "good-owner", model="anthropic/claude-3-5-sonnet")
        seed_thread(db, "author")
        db.execute("UPDATE models SET harness='invalid' WHERE id='openai/test-checkpoint'")
        bad_document = seed_document(db, "bad-owner", name="bad.md")
        good_document = seed_document(db, "good-owner", name="good.md")
        add_comment(db, bad_document)
        add_comment(db, good_document)
        db.execute("UPDATE doc_feedback_batches SET closes_at='2000-01-01T00:00:00+00:00'")
        db.commit()

        feedback_batches.close_due("2000-01-01T00:00:00+00:00", db=db)

        by_document = {row["artifact_id"]: row for row in batches(db)}
        assert (by_document[bad_document]["state"], by_document[bad_document]["undelivered_reason"]) == (
            "undelivered", "owner_unaddressable",
        )
        assert (by_document[good_document]["state"], by_document[good_document]["delivered_node_id"]) == (
            "delivered", _feedback_nodes(db)[0]["id"],
        )
        assert len(_feedback_nodes(db)) == 1
    finally:
        db.close()
