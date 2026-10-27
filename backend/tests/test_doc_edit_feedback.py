"""DCOL-FR5.4 substantive non-owner edit feedback delivery."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from doc_feedback_helpers import add_comment, batches, seed_document, seed_thread
from doceditor import feedback_batch_edits, feedback_batches
from doceditor.document_scope import MAX_TEXT_BYTES
from infra.database import get_db, write_transaction
from plans import admission, service
from plans.feedback_nodes import (
    COMMENT_ACCEPTANCE,
    EDIT_ACCEPTANCE,
    EmptyFeedbackBatchError,
    create_feedback_node,
)
from plans.feedback_items import load_feedback_items
from registry import models as registry_models
from registry.identity_metadata import merge_identity_metadata
from registry.shared_document_events import record_post_write_events
from registry.shared_target import SharedTarget
from registry.shared_edit_classification import DIFF_CAP_BYTES
from registry.shared_principals import WriteAuthority

PATH = Path("/workspace/shared/feedback.md")
T0 = "2026-09-30T08:00:00+00:00"


def _conductor_edit(owner: str) -> WriteAuthority:
    """An allowed non-owner write: the conductor edits a document another agent owns."""
    return WriteAuthority("conductor", owner, "doc.md", "author")


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _set_current(db, artifact_id: str, content: bytes) -> None:
    merge_identity_metadata(db, artifact_id, {"last_written_sha256": _sha(content)})


def _edit(
    db,
    artifact_id: str,
    before: bytes,
    after: bytes,
    *,
    author_id: str = "author",
    author_kind: str = "agent",
    display: str = "Editor",
    recipient: str = "owner",
    at: str = T0,
) -> str:
    db.commit()
    with write_transaction(db, scope="test_record_edit") as connection:
        batch_id = feedback_batch_edits.record_edit(
            connection,
            artifact_id=artifact_id,
            recipient_id=recipient,
            author_kind=author_kind,
            author_id=author_id,
            actor_display=display,
            path=PATH,
            before=before,
            after=after,
            at=at,
        )
    _set_current(db, artifact_id, after)
    db.commit()
    return batch_id


def _node_for_batch(db, batch_id: str):
    batch = db.execute(
        "SELECT * FROM doc_feedback_batches WHERE id=?", (batch_id,)
    ).fetchone()
    return service.get_node(batch["delivered_node_id"], db=db)


def test_six_contiguous_saves_coalesce_to_one_item_and_one_baseline_diff(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author", title="Conductor")
        artifact_id = seed_document(db, "owner")
        states = [f"version {index}\n".encode() for index in range(7)]
        batch_id = ""
        for index in range(6):
            at = (datetime.fromisoformat(T0) + timedelta(seconds=index)).isoformat()
            batch_id = _edit(db, artifact_id, states[index], states[index + 1], at=at)

        batch = batches(db, state="open")[0]
        row = db.execute(
            "SELECT * FROM doc_feedback_edit_items WHERE batch_id=?", (batch_id,)
        ).fetchone()
        item = load_feedback_items(db, batch)[0]
        assert (batch["id"], batch["item_count"], row["save_count"]) == (batch_id, 1, 6)
        assert row["prior_segments_diff"] == ""
        assert "-version 0" in item["rendered_diff"]
        assert "+version 6" in item["rendered_diff"]
        assert "version 3" not in item["rendered_diff"]
    finally:
        db.close()


def test_two_editors_never_render_each_others_bytes_and_later_owner_save_diverges(
    isolated_db, tmp_path,
):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "conductor", title="Conductor")
        artifact_id = seed_document(db, "owner")
        current_path = tmp_path / "feedback.md"
        db.execute(
            "UPDATE artifact_identities SET path=? WHERE id=?",
            (str(current_path), artifact_id),
        )
        conductor_batch = _edit(
            db, artifact_id, b"A\n", b"B\n", author_id="conductor", display="Conductor", at=T0,
        )
        user_batch = _edit(
            db, artifact_id, b"B\n", b"C\n", author_id="ui", author_kind="user",
            display="Steve", at="2026-09-30T08:00:01+00:00",
        )
        assert _edit(
            db, artifact_id, b"C\n", b"D\n", author_id="conductor", display="Conductor",
            at="2026-09-30T08:00:02+00:00",
        ) == conductor_batch

        by_id = {row["id"]: row for row in batches(db, state="open")}
        conductor = load_feedback_items(db, by_id[conductor_batch])[0]
        user = load_feedback_items(db, by_id[user_batch])[0]
        assert all(token in conductor["rendered_diff"] for token in ("-A", "+B", "-C", "+D"))
        assert "-B\n+C" not in conductor["rendered_diff"]
        assert "-B" in user["rendered_diff"] and "+C" in user["rendered_diff"]

        current_path.write_bytes(b"E\n")
        feedback_batches.close_batch(db, conductor_batch, "quiet")
        description = _node_for_batch(db, conductor_batch).description
        assert "The document has changed since this edit" in description
        assert "+D" in description and "+E" not in description
    finally:
        db.close()


def test_mechanical_event_stays_passive_and_adds_no_edit_item(isolated_db, tmp_path):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner")
        artifact = registry_models.get_artifact(artifact_id, db=db)
        path = tmp_path / "doc.md"
        shared = SharedTarget(
            path, tmp_path, tmp_path / "INDEX.md", False,
            actor_principal="author", owner_thread_id="owner", classification="mechanical",
            authority=_conductor_edit("owner"),
        )

        record_post_write_events(
            shared, artifact, before=b"See [A](old.md)\n", after=b"See [A](new.md)\n",
            actor_thread_id="author", db=db,
        )

        assert db.execute("SELECT COUNT(*) FROM doc_feedback_edit_items").fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM context_change_journal WHERE object_kind='artifact' "
            "AND object_id=? AND change_kind='content_edited_by_non_owner'",
            (artifact_id,),
        ).fetchone()[0] == 1
    finally:
        db.close()


@pytest.mark.parametrize(
    ("before", "after", "empty_owner"),
    [
        pytest.param(
            b"old\n", b"x" * (MAX_TEXT_BYTES + 1), False, id="over-limit",
        ),
        pytest.param(b"old\n", b"new\xff\n", False, id="non-utf8"),
        pytest.param(b"old\n", b"new\n", True, id="no-owner"),
    ],
)
def test_unrenderable_content_event_preserves_write_as_passive_note(
    isolated_db, tmp_path, before, after, empty_owner,
):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner")
        artifact = registry_models.get_artifact(artifact_id, db=db)
        if empty_owner:
            db.execute(
                "UPDATE artifact_registrations SET parent_thread_id=' ' WHERE artifact_id=?",
                (artifact_id,),
            )
        path = tmp_path / "doc.md"
        path.write_bytes(after)
        shared = SharedTarget(
            path, tmp_path, tmp_path / "INDEX.md", False,
            actor_principal="author",
            owner_thread_id=" " if empty_owner else "owner",
            classification="content",
            authority=_conductor_edit(" " if empty_owner else "owner"),
        )

        record_post_write_events(
            shared, artifact, before=before, after=after,
            actor_thread_id="author", db=db,
        )

        assert path.read_bytes() == after
        assert db.execute("SELECT COUNT(*) FROM doc_feedback_edit_items").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM doc_feedback_batches").fetchone()[0] == 0
        notes = db.execute(
            "SELECT object_kind,object_id,change_kind FROM context_change_journal "
            "WHERE object_kind='artifact' AND object_id=? "
            "AND change_kind='content_edited_by_non_owner'",
            (artifact_id,),
        ).fetchall()
        assert len(notes) == 1
    finally:
        db.close()


def test_content_event_routes_to_edit_writer_without_passive_artifact_note(isolated_db, tmp_path):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author", title="Conductor")
        artifact_id = seed_document(db, "owner")
        artifact = registry_models.get_artifact(artifact_id, db=db)
        path = tmp_path / "doc.md"
        shared = SharedTarget(
            path, tmp_path, tmp_path / "INDEX.md", False,
            actor_principal="author", owner_thread_id="owner", classification="content",
            authority=_conductor_edit("owner"),
        )

        db.commit()
        with write_transaction(db, scope="test_record_post_write_events") as connection:
            record_post_write_events(
                shared, artifact, before=b"old\n", after=b"new\n",
                actor_thread_id="author", db=connection,
            )

        assert db.execute("SELECT COUNT(*) FROM doc_feedback_edit_items").fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM context_change_journal WHERE object_kind='artifact' "
            "AND object_id=? AND change_kind='content_edited_by_non_owner'",
            (artifact_id,),
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_revert_to_baseline_is_typed_empty_without_node_notes_admission_or_wake(
    isolated_db, monkeypatch,
):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner")
        batch_id = _edit(db, artifact_id, b"A\n", b"B\n")
        _edit(db, artifact_id, b"B\n", b"A\n", at="2026-09-30T08:00:01+00:00")

        def forbidden(*_args, **_kwargs):
            raise AssertionError("empty delivery must not run admission")

        monkeypatch.setattr(admission, "reconcile_plan", forbidden)
        closed, woken = feedback_batches.close_batch(db, batch_id, "quiet")

        batch = batches(db)[0]
        assert closed is True and woken == ()
        assert (batch["state"], batch["delivered_node_id"], batch["delivery_outcome"]) == (
            "delivered", None, "empty",
        )
        assert db.execute("SELECT COUNT(*) FROM plan_nodes").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM context_change_journal").fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM doc_feedback_edit_items WHERE batch_id=?", (batch_id,),
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_node_and_ui_successes_record_typed_outcome_and_delete_edit_evidence(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        first = seed_document(db, "owner", name="node.md")
        second = seed_document(db, "owner", name="ui.md")
        node_batch = _edit(db, first, b"old\n", b"new\n")
        ui_batch = _edit(db, second, b"old\n", b"new\n", recipient="ui")

        feedback_batches.close_batch(db, node_batch, "quiet")
        feedback_batches.close_batch(db, ui_batch, "quiet")

        outcomes = {row["id"]: row for row in batches(db)}
        assert outcomes[node_batch]["delivery_outcome"] == "node"
        assert outcomes[node_batch]["delivered_node_id"] is not None
        assert outcomes[ui_batch]["delivery_outcome"] == "ui"
        assert outcomes[ui_batch]["delivered_node_id"] is None
        assert db.execute("SELECT COUNT(*) FROM doc_feedback_edit_items").fetchone()[0] == 0
    finally:
        db.close()


def test_delivered_edit_only_batch_stays_idempotent_after_evidence_is_deleted(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner")
        batch_id = _edit(db, artifact_id, b"old\n", b"new\n")
        feedback_batches.close_batch(db, batch_id, "quiet")
        delivered = next(row for row in batches(db) if row["id"] == batch_id)
        assert load_feedback_items(db, delivered) == []

        again = create_feedback_node(db, "owner", delivered)

        assert (again.node_id, again.created) == (delivered["delivered_node_id"], False)
        assert db.execute(
            "SELECT COUNT(*) FROM plan_node_artifact_links WHERE source_event_id=?",
            (batch_id,),
        ).fetchone()[0] == 1
    finally:
        db.close()


def test_mixed_batch_orders_items_and_renders_conditional_title_acceptance_and_ids(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author", title="Reviewer")
        artifact_id = seed_document(db, "owner", name="mixed.md")
        first, _ = add_comment(db, artifact_id)
        second, _ = add_comment(db, artifact_id)
        db.execute("UPDATE doc_comments SET created_at=? WHERE id=?", (T0, first["id"]))
        db.execute(
            "UPDATE doc_comments SET created_at=? WHERE id=?",
            ("2026-09-30T08:00:02+00:00", second["id"]),
        )
        batch_id = _edit(
            db, artifact_id, b"old\n", b"new\n", at="2026-09-30T08:00:01+00:00",
        )

        feedback_batches.close_batch(db, batch_id, "quiet")
        node = _node_for_batch(db, batch_id)

        assert node.title == "Reviewer edited mixed and left 2 comments"
        assert node.acceptance_criteria == f"{COMMENT_ACCEPTANCE} {EDIT_ACCEPTANCE}"
        first_at = node.description.index(f"Comment {first['id']}")
        edit_at = node.description.index("Edit by Editor")
        second_at = node.description.index(f"Comment {second['id']}")
        assert first_at < edit_at < second_at
        assert node.description.count("Comment ") == 2
        assert first["id"] in node.description and second["id"] in node.description
        queued = db.execute(
            "SELECT summary FROM context_change_journal WHERE object_id='owner' ORDER BY seq DESC"
        ).fetchone()["summary"]
        assert queued == (
            "Feedback from Reviewer on mixed (1 edit, 2 comments) is queued in your plan; "
            "nothing to do now."
        )
    finally:
        db.close()


def test_edit_only_user_batch_uses_actor_display_and_applied_edit_framing(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact_id = seed_document(db, "owner", name="intent.md")
        batch_id = _edit(
            db, artifact_id, b"old\n", b"new\n", author_id="ui", author_kind="user",
            display="Steve",
        )

        feedback_batches.close_batch(db, batch_id, "send")
        node = _node_for_batch(db, batch_id)

        assert node.title == "Steve edited intent"
        assert node.acceptance_criteria == EDIT_ACCEPTANCE
        assert node.description.startswith(
            "Edit by Steve (1 save, 2026-09-30T08:00:00+00:00 to "
            "2026-09-30T08:00:00+00:00), already applied:"
        )
        assert "The user changed this directly and it is already applied." in node.description
        assert "once the change lands" not in node.description
        assert "Comment " not in node.description
        assert node.intent is None
        assert db.execute(
            "SELECT COUNT(*) FROM plan_node_intent_words WHERE node_id=?", (node.id,)
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_undelivered_edit_retains_evidence_and_retry_deletes_it(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner", model="missing/model")
        seed_thread(db, "author", title="Conductor")
        artifact_id = seed_document(db, "owner", name="retry-edit.md")
        batch_id = _edit(db, artifact_id, b"old\n", b"new\n")

        feedback_batches.close_batch(db, batch_id, "quiet")
        failed = batches(db)[0]
        assert (failed["state"], failed["delivery_outcome"]) == ("undelivered", None)
        assert db.execute(
            "SELECT COUNT(*) FROM doc_feedback_edit_items WHERE batch_id=?", (batch_id,),
        ).fetchone()[0] == 1
        note = db.execute(
            "SELECT summary FROM context_change_journal WHERE object_id='author' ORDER BY seq DESC"
        ).fetchone()["summary"]
        assert note == (
            "1 edit on /workspace/shared/retry-edit.md not delivered: "
            f'owner unaddressable. Retry with doc_feedback_send(artifact_id="{artifact_id}") '
            "once the owner can receive it."
        )

        db.execute("UPDATE threads SET model='openai/test-checkpoint' WHERE id='owner'")
        db.commit()
        feedback_batches.retry_batch(db, batch_id)
        node = _node_for_batch(db, batch_id)
        assert "-old" in node.description and "+new" in node.description
        assert batches(db)[0]["delivery_outcome"] == "node"
        assert db.execute(
            "SELECT COUNT(*) FROM doc_feedback_edit_items WHERE batch_id=?", (batch_id,),
        ).fetchone()[0] == 0
    finally:
        db.close()


def test_edit_admission_queues_when_busy_and_wakes_when_idle(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "busy-owner")
        seed_thread(db, "idle-owner")
        seed_thread(db, "author")
        busy_plan = service.ensure_plan("thread", "busy-owner", db=db)
        current = service.add_node(
            busy_plan.id, kind="task", title="Current work",
            acceptance_criteria="Current work is complete.", db=db,
        )
        service.transition_node(current.id, "active", db=db)
        admission.reconcile_plan(busy_plan.id, actor_id=None, provenance="test", db=db)

        busy_doc = seed_document(db, "busy-owner", name="busy.md")
        busy_batch = _edit(
            db, busy_doc, b"old\n", b"new\n", recipient="busy-owner",
        )
        _, busy_woken = feedback_batches.close_batch(db, busy_batch, "quiet")
        busy_node = _node_for_batch(db, busy_batch)
        assert busy_woken == ()
        assert db.execute(
            "SELECT 1 FROM plan_node_attempts WHERE node_id=? AND released_at IS NULL",
            (busy_node.id,),
        ).fetchone() is None
        assert db.execute(
            "SELECT executor_thread_id FROM plan_node_attempts "
            "WHERE node_id=? AND released_at IS NULL", (current.id,),
        ).fetchone()["executor_thread_id"] == "busy-owner"

        idle_doc = seed_document(db, "idle-owner", name="idle.md")
        idle_batch = _edit(
            db, idle_doc, b"old\n", b"new\n", recipient="idle-owner",
        )
        _, idle_woken = feedback_batches.close_batch(db, idle_batch, "quiet")
        idle_node = _node_for_batch(db, idle_batch)
        attempt = db.execute(
            "SELECT executor_thread_id FROM plan_node_attempts "
            "WHERE node_id=? AND released_at IS NULL", (idle_node.id,),
        ).fetchone()
        assert idle_woken == ("idle-owner",)
        assert attempt["executor_thread_id"] == "idle-owner"
    finally:
        db.close()


def test_composed_diff_cap_and_zero_item_guard(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        seed_thread(db, "author")
        artifact_id = seed_document(db, "owner")
        before = ("old line\n" * 30_000).encode()
        middle = ("middle line\n" * 30_000).encode()
        after = ("new line\n" * 30_000).encode()
        batch_id = _edit(db, artifact_id, before, middle)
        _edit(db, artifact_id, b"intervening\n", after, at="2026-09-30T08:00:01+00:00")
        batch = batches(db, state="open")[0]
        rendered = load_feedback_items(db, batch)[0]["rendered_diff"]
        assert "diff truncated" in rendered
        assert len(rendered.encode("utf-8")) <= DIFF_CAP_BYTES + 64

        empty_batch = {
            "id": "zero-items",
            "artifact_id": artifact_id,
            "author_kind": "agent",
            "author_id": "author",
        }
        with pytest.raises(EmptyFeedbackBatchError):
            create_feedback_node(db, "owner", empty_batch)
        seed_thread(db, "planless-owner")
        with pytest.raises(EmptyFeedbackBatchError):
            create_feedback_node(db, "planless-owner", empty_batch)
        assert db.execute(
            "SELECT COUNT(*) FROM plan_node_artifact_links WHERE source_event_id='zero-items'"
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM plans WHERE owner_kind='thread' AND owner_id='planless-owner'"
        ).fetchone()[0] == 0
        assert batch_id == batch["id"]
    finally:
        db.close()
