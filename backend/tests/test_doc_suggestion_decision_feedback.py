"""DCOL-FR10.2 routes suggestion decisions back to their agent author."""

from __future__ import annotations

import json

import pytest

from doc_feedback_helpers import add_comment, batches, seed_document, seed_thread
from doceditor import comment_actions, feedback_batches, feedback_state, feedback_suggestion_decisions
from doceditor.comment_action_errors import decision_only_error
from doceditor.comment_resolve_rule import PERSON_COMMENT_RESOLVE_RULE
from infra.database import get_db, write_transaction
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_VERSION
from tests.schema_rewind import rewind_to_v88_policy
from plans import lifecycle, service
from plans.feedback_nodes import USER_FRAMING


def _decide(db, suggestion: dict, decision: str, reason: str | None = None) -> dict:
    with write_transaction(db, scope="test_suggestion_decision_feedback") as connection:
        return comment_actions.record_decision(
            suggestion["id"],
            decision=decision,
            decided_by="Robin",
            decided_by_thread_id=None,
            reason=reason,
            db=connection,
        )


def _user_batch_for(db, recipient_id: str) -> dict:
    return next(
        batch
        for batch in batches(db, state="open")
        if batch["author_kind"] == "user" and batch["recipient_id"] == recipient_id
    )


def _notes(db, thread_id: str) -> list[str]:
    return [
        row["summary"] for row in db.execute(
            "SELECT summary FROM context_change_journal "
            "WHERE object_kind='thread_context' AND object_id=? ORDER BY seq", (thread_id,))
    ]


def _accepted_note(suggestion: dict) -> str:
    """Names the accepted suggestion and quotes the one resolve rule, never a second wording of it."""
    return f"Robin accepted your suggestion on Design: “quoted source” ({suggestion['id']}). {PERSON_COMMENT_RESOLVE_RULE}"


def test_an_accepted_suggestion_is_named_by_its_quote_clipped_to_one_line():
    from doceditor.feedback_suggestion_decisions import QUOTE_CHARS, _named

    named = _named({"id": "s1", "anchor_text": "word " * 30 + "\nend"})
    quote = named[1:named.index("”")]
    assert "\n" not in quote and len(quote) == QUOTE_CHARS and quote.endswith("…")
    assert named.endswith(" (s1)")
    assert _named({"id": "s2", "anchor_text": "  \n"}) == "s2"


def test_an_accept_all_note_names_every_suggestion_and_keeps_the_persons_line_short(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner", title="Documents")
        seed_thread(db, "author", title="Reviewer")
        artifact_id = seed_document(db, "owner", name="Design.md")
        suggestions = [add_comment(db, artifact_id, replacement=f"New {n}")[0] for n in range(7)]

        feedback_suggestion_decisions.record_suggestion_decisions(
            db, suggestions, decision="accepted", decided_by="Robin", decided_by_thread_id=None,
            decision_event_id="event-1", reason=None,
        )

        note = db.execute(
            "SELECT summary, details_json FROM context_change_journal "
            "WHERE object_kind='thread_context' AND object_id='author'"
        ).fetchone()
        named = ", ".join(f"“quoted source” ({s['id']})" for s in suggestions)
        assert note["summary"] == (
            f"Robin accepted 7 of your suggestions on Design: {named}. {PERSON_COMMENT_RESOLVE_RULE}"
        )
        details = json.loads(note["details_json"])
        assert details["chip"] == "Robin accepted 7 suggestions on Design."
    finally:
        db.close()


def _plan_nodes(db, thread_id: str) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM plan_nodes n JOIN plans p ON p.id=n.plan_id "
        "WHERE p.owner_kind='thread' AND p.owner_id=?", (thread_id,)).fetchone()[0]


def test_rejection_is_a_passive_note_and_never_joins_the_deciders_reply_batch(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner", title="Documents")
        seed_thread(db, "author", title="Reviewer")
        artifact_id = seed_document(db, "owner", name="Design.md")
        suggestion, _ = add_comment(db, artifact_id, replacement="Better wording")
        reply, _ = add_comment(
            db, artifact_id, author_kind="user", author_id=None, author="Robin",
            parent_id=suggestion["id"],
        )

        _decide(db, suggestion, "reject")

        assert _notes(db, "author") == ["Robin rejected your suggestion on Design."]
        assert _plan_nodes(db, "author") == 0
        # The user's reply remains ordinary document feedback, but the decision never joins its batch.
        batch = _user_batch_for(db, "author")
        assert db.execute(
            "SELECT event FROM doc_feedback_batch_items WHERE batch_id=? AND comment_id=?",
            (batch["id"], suggestion["id"]),
        ).fetchone() is None
        assert reply["id"]
    finally:
        db.close()


def test_rejection_note_carries_the_deciders_reason_verbatim(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner", title="Documents")
        seed_thread(db, "author", title="Reviewer")
        artifact_id = seed_document(db, "owner", name="Design.md")
        suggestion, _ = add_comment(db, artifact_id, replacement="Better wording")

        _decide(db, suggestion, "reject", reason="Out of scope: see section 2.")

        assert _notes(db, "author") == [
            "Robin rejected your suggestion on Design. "
            "Reason from the decider (verbatim): Out of scope: see section 2."
        ]
        details = json.loads(db.execute(
            "SELECT details_json FROM context_change_journal "
            "WHERE object_kind='thread_context' AND object_id='author'").fetchone()[0])
        assert details["chip"] == "Robin rejected a suggestion on Design."
        assert _plan_nodes(db, "author") == 0
    finally:
        db.close()


def test_mixed_batch_keeps_the_comment_framing_and_a_rejection_adds_no_node(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner", title="Documents")
        seed_thread(db, "author", title="Reviewer")
        artifact_id = seed_document(db, "owner", name="Design.md")
        suggestion, _ = add_comment(db, artifact_id, replacement="Better wording")
        remark, _ = add_comment(db, artifact_id)
        add_comment(
            db, artifact_id, author_kind="user", author_id=None, author="Robin",
            parent_id=remark["id"],
        )

        _decide(db, suggestion, "reject")

        assert _plan_nodes(db, "author") == 0
        batch = _user_batch_for(db, "author")
        assert feedback_batches.close_batch(db, batch["id"], "send") == (True, ("author",))
        delivered = next(row for row in batches(db) if row["id"] == batch["id"])
        node = service.get_node(delivered["delivered_node_id"], db=db)
        assert node.description.startswith(USER_FRAMING)
        assert _plan_nodes(db, "author") == 1
    finally:
        db.close()


def test_rejection_keeps_the_suggesters_settled_source_task_settled_and_is_acknowledgeable(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner")
        suggestion, _ = add_comment(db, artifact_id, replacement="Replacement")
        source_plan = service.ensure_plan("thread", "author", db=db)
        source = service.add_node(
            source_plan.id, kind="task", title="Finished suggestion",
            acceptance_criteria="Finished.", db=db,
        )
        service.transition_node(source.id, "active", actor_id=None, db=db)
        service.transition_node(source.id, "closed", actor_id=None, db=db)

        _decide(db, suggestion, "reject")

        assert service.get_node(source.id, db=db).state == "closed"
        assert _plan_nodes(db, "author") == 1
        comment_actions.check_agent_update(
            suggestion, {"acked": True}, actor_thread_id="author",
            owner_thread_id="owner", db=db,
        )
    finally:
        db.close()


def test_rejection_queues_no_user_feedback_and_no_plan_work(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner", title="Documents")
        seed_thread(db, "author", title="Reviewer")
        artifact_id = seed_document(db, "owner", name="Design.md")
        suggestion, _ = add_comment(db, artifact_id, replacement="Replacement")

        _decide(db, suggestion, "reject")

        assert [
            batch for batch in batches(db, state="open")
            if batch["author_kind"] == "user" and batch["recipient_id"] == "author"
        ] == []
        assert _plan_nodes(db, "author") == 0
        assert len(_notes(db, "author")) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_bulk_rejection_sends_one_note_for_all_suggestions_without_a_node_or_user_batch(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner", title="Documents")
        seed_thread(db, "author", title="Reviewer")
        artifact_id = seed_document(db, "owner", name="Design.md")
        first, _ = add_comment(db, artifact_id, replacement="First")
        add_comment(db, artifact_id, replacement="Second")

        async def unused_writer(*_args):
            raise AssertionError("reject must not write the document")

        await comment_actions.decide_all_from_author(
            first["id"], "reject", user_id="owner-token", decided_by="Robin",
            actor_thread_id=None, write=unused_writer,
        )

        assert [
            batch for batch in batches(db, state="open")
            if batch["author_kind"] == "user" and batch["recipient_id"] == "author"
        ] == []
        assert _notes(db, "author") == ["Robin rejected 2 of your suggestions on Design."]
        assert _plan_nodes(db, "author") == 0
    finally:
        db.close()


def test_acceptance_writes_one_passive_note_without_creating_or_waking_plan_work(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner", title="Documents")
        seed_thread(db, "author", title="Reviewer")
        artifact_id = seed_document(db, "owner", name="Design.md")
        suggestion, _ = add_comment(db, artifact_id, replacement="Replacement")
        paused_plan = service.ensure_plan("thread", "author", db=db)
        lifecycle.transition(
            paused_plan.id,
            "paused",
            expected_revision=paused_plan.revision,
            actor_id="author",
            provenance="test",
            db=db,
        )
        db.commit()

        comment_actions.record_decision(
            suggestion["id"],
            decision="accept",
            decided_by="Robin",
            decided_by_thread_id=None,
            db=db,
        )

        assert [
            batch for batch in batches(db, state="open")
            if batch["author_kind"] == "user" and batch["recipient_id"] == "author"
        ] == []
        note = db.execute(
            "SELECT summary FROM context_change_journal WHERE object_kind='thread_context' AND object_id='author'"
        ).fetchone()
        assert note["summary"] == _accepted_note(suggestion)
        plan = service.ensure_plan("thread", "author", db=db)
        assert plan.state == "paused"
        assert db.execute("SELECT COUNT(*) FROM plan_nodes WHERE plan_id=?", (plan.id,)).fetchone()[0] == 0
    finally:
        db.close()


def test_agent_self_decision_creates_no_feedback_batch_or_node(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact_id = seed_document(db, "owner")
        suggestion, feedback = add_comment(
            db,
            artifact_id,
            author_id="owner",
            author="Owner",
            replacement="Replacement",
        )
        assert feedback["batch_ids"] == []

        with write_transaction(db, scope="test_self_suggestion_decision") as connection:
            comment_actions.record_decision(
                suggestion["id"],
                decision="reject",
                decided_by="Owner",
                decided_by_thread_id="owner",
                db=connection,
            )

        assert batches(db, state="open") == []
        assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 0
    finally:
        db.close()


def test_retired_suggester_rejection_stays_undelivered(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner")
        suggestion, _ = add_comment(db, artifact_id, replacement="Replacement")
        db.execute("DELETE FROM threads WHERE id='author'")
        db.commit()
        _decide(db, suggestion, "reject")

        assert [batch for batch in batches(db) if batch["author_kind"] == "user"] == []
        assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 0
    finally:
        db.close()


def test_user_decision_does_not_increment_document_comment_send_count(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner", title="Documents")
        seed_thread(db, "author", title="Reviewer")
        artifact_id = seed_document(db, "owner", name="Design.md")
        suggestion, _ = add_comment(db, artifact_id, replacement="Replacement")

        _decide(db, suggestion, "reject")

        assert feedback_state.user_feedback_for_document(db, artifact_id)["open_count"] == 0
    finally:
        db.close()


def test_unaddressable_suggester_creates_no_batch_or_retry_state(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner")
        suggestion, _ = add_comment(db, artifact_id, replacement="Replacement")
        db.execute("DELETE FROM threads WHERE id='author'")
        db.commit()

        _decide(db, suggestion, "reject")

        assert [
            batch for batch in batches(db)
            if batch["author_kind"] == "user" and batch["recipient_id"] == "author"
        ] == []
        assert feedback_state.undelivered_for_document(
            db, author_kind="user", author_id="ui", artifact_id=artifact_id,
        ) == []
    finally:
        db.close()


def test_acceptance_is_immediately_passive_and_does_not_wake_a_paused_suggester(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner", title="Documents")
        seed_thread(db, "author", title="Reviewer")
        artifact_id = seed_document(db, "owner", name="Design.md")
        suggestion, _ = add_comment(db, artifact_id, replacement="Replacement")
        plan = service.ensure_plan("thread", "author", db=db)
        lifecycle.transition(
            plan.id, "paused", expected_revision=plan.revision, actor_id="author", provenance="test", db=db,
        )
        db.commit()

        _decide(db, suggestion, "accept")

        assert [
            batch for batch in batches(db, state="open")
            if batch["author_kind"] == "user" and batch["recipient_id"] == "author"
        ] == []
        note = db.execute(
            "SELECT summary FROM context_change_journal WHERE object_kind='thread_context' AND object_id='author'"
        ).fetchone()
        assert note["summary"] == _accepted_note(suggestion)
        assert service.ensure_plan("thread", "author", db=db).state == "paused"
        assert db.execute("SELECT COUNT(*) FROM plan_nodes WHERE plan_id=?", (plan.id,)).fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_direct_redecision_is_refused_until_the_rejected_suggestion_reopens(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner")
        suggestion, _ = add_comment(db, artifact_id, replacement="Replacement")

        async def unused_writer(*_args):
            raise AssertionError("reject must not write the document")

        await comment_actions.decide_suggestion(
            suggestion["id"], "reject", user_id="owner-token", decided_by="Robin",
            actor_thread_id=None, write=unused_writer,
        )
        with pytest.raises(comment_actions.CommentActionError, match="Re-open a rejected suggestion"):
            await comment_actions.decide_suggestion(
                suggestion["id"], "accept", user_id="owner-token", decided_by="Robin",
                actor_thread_id=None, write=unused_writer,
            )
    finally:
        db.close()


def test_migration_preserves_reopened_rows_and_admits_rejected_rows(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner")
        comment, _ = add_comment(db, artifact_id)
        batch = batches(db, state="open")[0]
        db.execute("UPDATE doc_feedback_batch_items SET event='reopened' WHERE batch_id=?", (batch["id"],))
        db.execute("DROP INDEX idx_doc_feedback_batch_items_comment")
        db.execute("ALTER TABLE doc_feedback_batch_items RENAME TO doc_feedback_batch_items_v83")
        db.execute(
            """CREATE TABLE doc_feedback_batch_items (
                batch_id TEXT NOT NULL REFERENCES doc_feedback_batches(id) ON DELETE CASCADE,
                comment_id TEXT NOT NULL REFERENCES doc_comments(id) ON DELETE CASCADE,
                event TEXT CHECK (event IS NULL OR event='reopened'),
                event_at TEXT,
                event_by TEXT,
                PRIMARY KEY (batch_id, comment_id)
            )"""
        )
        db.execute(
            """INSERT INTO doc_feedback_batch_items(batch_id,comment_id,event,event_at,event_by)
               SELECT batch_id,comment_id,event,event_at,event_by FROM doc_feedback_batch_items_v83"""
        )
        db.execute("DROP TABLE doc_feedback_batch_items_v83")
        db.execute("CREATE INDEX idx_doc_feedback_batch_items_comment ON doc_feedback_batch_items(comment_id)")
        rewind_to_v88_policy(db)
        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("PRAGMA user_version=82")
        db.commit()

        assert migrate_to_current(db, SCHEMA_VERSION).migrated
        assert SCHEMA_VERSION == 133
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert db.execute(
            "SELECT event FROM doc_feedback_batch_items WHERE comment_id=?", (comment["id"],)
        ).fetchone()[0] == "reopened"
        # Decision events are no longer valid feedback-batch items.
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("bulk", [False, True])
async def test_the_decide_path_wakes_nobody_for_a_rejection(isolated_db, monkeypatch, bulk):
    from plans import public

    driven: list[tuple[str, ...]] = []

    async def drive_threads(thread_ids):
        driven.append(tuple(thread_ids))

    monkeypatch.setattr(public, "drive_threads", drive_threads)
    db = get_db()
    try:
        seed_thread(db, "owner", title="Documents")
        seed_thread(db, "author", title="Reviewer")
        artifact_id = seed_document(db, "owner", name="Design.md")
        suggestion, _ = add_comment(db, artifact_id, replacement="Replacement")

        async def unused_writer(*_args):
            raise AssertionError("reject must not write the document")

        decide = comment_actions.decide_all_from_author if bulk else comment_actions.decide_suggestion
        result = await decide(
            suggestion["id"], "reject", user_id="owner-token", decided_by="Robin",
            actor_thread_id=None, write=unused_writer,
        )

        assert driven == []
        assert not any(str(key).startswith("_") for key in result)
    finally:
        db.close()


@pytest.mark.parametrize(("fields", "refusal"), [
    ({"decision": None, "all_from_author": True, "reason": None}, "all_from_author requires decision"),
    ({"decision": None, "all_from_author": False, "reason": ""}, "reason requires decision"),
    ({"decision": "reject", "all_from_author": True, "reason": "Why"}, None),
    ({"decision": None, "all_from_author": False, "reason": None}, None),
])
def test_decision_only_fields_need_a_decision(fields, refusal):
    assert decision_only_error(**fields) == refusal
