"""A reply aimed at another reply joins its thread instead of disappearing."""

from __future__ import annotations

from doc_feedback_helpers import add_comment, seed_document, seed_thread
from doceditor import comments, feedback_state
from infra.database import get_db


def _thread(db):
    seed_thread(db, "owner")
    db.execute("UPDATE threads SET user_id='reader-user' WHERE id='owner'")
    artifact_id = seed_document(db, "owner")
    root, _ = add_comment(db, artifact_id, author_kind="user", author="Reader")
    reply, _ = add_comment(db, artifact_id, author_kind="user", author="Reader", parent_id=root["id"])
    return artifact_id, root, reply


def test_reply_to_a_reply_is_stored_on_the_thread_root_and_listed(isolated_db):
    db = get_db()
    try:
        artifact_id, root, reply = _thread(db)
        nested, _ = add_comment(db, artifact_id, author_id="owner", author="Owner", parent_id=reply["id"])
        threads = comments.list_for_artifact(artifact_id, db=db)
        new_ids = feedback_state._new_replies(db, "reader-user", artifact_id)
    finally:
        db.close()

    assert nested["parent_id"] == root["id"]
    assert [[r["id"] for r in t["replies"]] for t in threads] == [[reply["id"], nested["id"]]]
    assert (root["id"], nested["id"]) in new_ids


def test_reply_already_stored_against_a_reply_still_shows_in_its_thread(isolated_db):
    db = get_db()
    try:
        artifact_id, root, reply = _thread(db)
        stranded, _ = add_comment(db, artifact_id, author_id="owner", author="Owner", parent_id=root["id"])
        db.execute("UPDATE doc_comments SET parent_id=? WHERE id=?", (reply["id"], stranded["id"]))
        db.commit()
        threads = comments.list_for_artifact(artifact_id, db=db)
    finally:
        db.close()

    assert [t["id"] for t in threads] == [root["id"]]
    assert [r["id"] for r in threads[0]["replies"]] == [reply["id"], stranded["id"]]
