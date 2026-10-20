"""DCOL-FR10.2: every route that stores a document's bytes tells the reviewers still waiting on it."""

from __future__ import annotations

import hashlib
import json

import pytest

from doceditor import review_requests as reviews
from doceditor.live_publisher import publish_one
from registry import service
from tests.test_document_live_publisher import owe, setup
from tests.test_shared_document_ownership import _write, ownership_scope as ownership_scope  # noqa: F401


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _artifact_id(scope, name):
    return service.find_file_identity(str(scope["category"] / name), db=scope["db"])["id"]


def _wait(scope, name, reviewer="reviewer-x", artifact_id=None):
    """A review request on the document with an open review task in Plans; returns the task's id."""
    from plans import public as plans_public

    db = scope["db"]
    db.execute(
        "INSERT OR IGNORE INTO threads (id,user_id,title,created_at,updated_at) "
        "SELECT ?,user_id,?,created_at,updated_at FROM threads WHERE id=?",
        (reviewer, "Reviewer", scope["owner"]),
    )
    plan = plans_public.ensure_plan("thread", reviewer, db=db)
    node = plans_public.add_node(plan.id, kind="task", title=f"Review {name}", acceptance_criteria="Reviewed", db=db)
    db.execute(
        "INSERT INTO doc_review_requests (id,artifact_id,requester_kind,requester_id,reviewer_thread_id,"
        "client_ids,created_at,node_id) VALUES (?,?,?,?,?,?,?,?)",
        (f"req-{name}", artifact_id or _artifact_id(scope, name), "agent", scope["owner"], reviewer, "[]",
         "2026-01-01T00:00:00+00:00", node.id),
    )
    db.commit()
    return node.id


def _edits(scope, name):
    rows = scope["db"].execute(
        "SELECT summary,actor_thread_id FROM context_change_journal "
        "WHERE object_id=? AND change_kind='content_edited' ORDER BY seq",
        (_artifact_id(scope, name),),
    ).fetchall()
    return [(row["summary"], row["actor_thread_id"]) for row in rows]


def test_a_durable_write_by_the_owner_is_journaled_for_the_reviewer(ownership_scope):
    scope = ownership_scope
    _write(scope, "doc.md", "one\n")
    _wait(scope, "doc.md")

    _write(scope, "doc.md", "two\n")

    assert _edits(scope, "doc.md") == [("Documents edited doc.", scope["owner"])]


def test_a_file_tool_registration_with_a_new_hash_is_journaled_and_a_resave_is_not(ownership_scope):
    scope = ownership_scope
    _write(scope, "tool.md", "one\n")
    _wait(scope, "tool.md")
    path = scope["category"] / "tool.md"
    path.write_text("two\n")

    service.register_write_strict(scope["owner"], str(path), metadata={"last_written_sha256": _sha("two\n")},
                                  db=scope["db"])
    service.register_write_strict(scope["owner"], str(path), metadata={"last_written_sha256": _sha("two\n")},
                                  db=scope["db"])

    assert len(_edits(scope, "tool.md")) == 1


def test_a_document_nobody_is_reviewing_journals_no_edit(ownership_scope):
    scope = ownership_scope
    _write(scope, "quiet.md", "one\n")

    _write(scope, "quiet.md", "two\n")

    assert _edits(scope, "quiet.md") == []


@pytest.mark.parametrize("settle", ["cancelled", "ended_without_comments"])
def test_a_settled_request_journals_no_edit(ownership_scope, settle):
    from plans import public as plans_public

    scope = ownership_scope
    _write(scope, "done.md", "one\n")
    node_id = _wait(scope, "done.md")
    if settle == "cancelled":
        from doceditor.review_lifecycle import transition

        transition(scope["db"], "req-done.md", "withdrawn")
    else:  # Neither cancelled nor answered: only Plans knows the review ended.
        plans_public.close_review_node(scope["db"], node_id, reason="Reviewed; nothing to add")
        assert not plans_public.review_is_open(scope["db"], node_id)
    scope["db"].commit()

    _write(scope, "done.md", "two\n")

    assert _edits(scope, "done.md") == []


def test_a_live_publication_names_the_editing_actor_not_the_registering_owner(ownership_scope, tmp_path):
    scope = ownership_scope
    store = setup(scope, tmp_path)
    _wait(scope, "live.md")
    owe(scope, store, "old", "new")

    publish_one(store, store.next_publication(), db=scope["db"])

    edits = _edits(scope, "live.md")
    assert len(edits) == 1 and edits[0][0].endswith("edited live.")


def test_a_user_save_with_no_thread_is_journaled_as_the_user(ownership_scope):
    from registry.review_edit_journal import journal_reviewed_edit

    scope = ownership_scope
    _write(scope, "saved.md", "one\n")
    _wait(scope, "saved.md")
    artifact = service.find_file_identity(str(scope["category"] / "saved.md"), db=scope["db"])

    def save(before: str, after: str) -> bool:
        return journal_reviewed_edit(
            scope["db"], artifact, previous_sha256=_sha(before), new_sha256=_sha(after), actor_thread_id="ui")

    assert save("one\n", "two\n")
    assert _edits(scope, "saved.md") == [("The user edited saved.", None)]
    assert not save("two\n", "two\n")


def test_a_plain_editor_save_with_no_thread_reaches_the_reviewer(ownership_scope):
    from doceditor.document_save import DocumentSave, _write_document

    scope = ownership_scope
    path = scope["workspace"] / "projects" / "alpha" / "notes.md"
    path.write_text("one\n")
    service.register_write_strict(scope["owner"], str(path), metadata={"last_written_sha256": _sha("one\n")},
                                  db=scope["db"])
    scope["db"].execute("UPDATE artifact_identities SET title='notes.md' WHERE path=?", (str(path),))
    scope["db"].commit()
    artifact = service.find_file_identity(str(path), db=scope["db"])
    _wait(scope, "notes.md", reviewer=scope["other"], artifact_id=artifact["id"])

    _write_document(DocumentSave(path=str(path), text="two\n"), path, direct_user=True)

    rows = scope["db"].execute(
        "SELECT summary FROM context_change_journal WHERE object_id=? AND change_kind='content_edited'",
        (artifact["id"],),
    ).fetchall()
    assert [row["summary"] for row in rows] == ["The user edited notes."]

    # A re-save of the same bytes is not an edit, even though no agent wrote them.
    _write_document(DocumentSave(path=str(path), text="two\n"), path, direct_user=True)
    rows = scope["db"].execute(
        "SELECT summary FROM context_change_journal WHERE object_id=? AND change_kind='content_edited'",
        (artifact["id"],),
    ).fetchall()
    assert len(rows) == 1


def test_a_user_save_through_a_thread_is_credited_to_the_user(ownership_scope):
    from doceditor.document_save import DocumentSave, _write_document

    scope = ownership_scope
    path = scope["workspace"] / "projects" / "alpha" / "threaded.md"  # A plain write, outside shared/.
    path.write_text("one\n")
    service.register_write_strict(scope["owner"], str(path), metadata={"last_written_sha256": _sha("one\n")},
                                  db=scope["db"])
    artifact_id = service.find_file_identity(str(path), db=scope["db"])["id"]
    _wait(scope, "threaded.md", artifact_id=artifact_id)

    _write_document(DocumentSave(path=str(path), text="two\n", thread_id=scope["owner"]), path, direct_user=True)

    rows = scope["db"].execute(
        "SELECT actor_thread_id FROM context_change_journal WHERE object_id=? AND change_kind='content_edited'",
        (artifact_id,),
    ).fetchall()
    assert [row["actor_thread_id"] for row in rows] == [None]  # The user, not the thread the canvas sits in.


def test_plan_titles_and_notes_name_a_document_by_one_rule(ownership_scope):
    from doceditor.note_names import document_name
    from plans.feedback_nodes import _artifact_name

    scope = ownership_scope
    db = scope["db"]
    _write(scope, "named.md", "one\n")
    artifact_id = _artifact_id(scope, "named.md")
    db.execute("UPDATE artifact_identities SET title=NULL,path=NULL,external_id='pull-42.md' WHERE id=?",
               (artifact_id,))
    row = db.execute("SELECT title,path,external_id FROM artifact_identities WHERE id=?", (artifact_id,)).fetchone()

    assert document_name(row) == _artifact_name(db, artifact_id) == "pull-42"
    assert document_name(None) == "the document" and _artifact_name(db, "missing") == "document"
