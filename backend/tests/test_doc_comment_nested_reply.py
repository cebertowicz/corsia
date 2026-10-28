"""Document-comment replies are stored exactly one level below their thread root."""

from __future__ import annotations

import sqlite3

from doc_feedback_helpers import add_comment, seed_document, seed_thread
from doceditor import comments, feedback_comments, feedback_state
from infra.database import get_db
from infra.database_migrations import MIGRATIONS
from infra.migration_141_to_142_doc_comment_reply_roots import migrate_141_to_142
from infra.schema import SCHEMA_VERSION


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
    finally:
        db.close()

    assert nested["parent_id"] == root["id"]
    assert [[r["id"] for r in thread["replies"]] for thread in threads] == [[reply["id"], nested["id"]]]


def test_migration_reroots_legacy_chains_and_leaves_roots_and_missing_parents_unchanged():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE doc_comments (id TEXT PRIMARY KEY, parent_id TEXT)")
    db.executemany(
        "INSERT INTO doc_comments(id,parent_id) VALUES (?,?)",
        [
            ("root", None),
            ("reply", "root"),
            ("nested", "reply"),
            ("deep", "nested"),
            ("unrelated-root", None),
            ("missing-parent", "absent"),
        ],
    )

    migrate_141_to_142(db)

    assert dict(db.execute("SELECT id,parent_id FROM doc_comments")) == {
        "root": None,
        "reply": "root",
        "nested": "root",
        "deep": "root",
        "unrelated-root": None,
        "missing-parent": "absent",
    }
    assert SCHEMA_VERSION == 162
    assert MIGRATIONS[141] is migrate_141_to_142


def test_reply_to_a_reply_notifies_the_root_comment_author(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact_id = seed_document(db, "owner")
        root, _ = add_comment(db, artifact_id, author_id="root-author", author="Root author")
        reply, _ = add_comment(db, artifact_id, author_id="middle-author", author="Middle author", parent_id=root["id"])
        nested, _ = add_comment(db, artifact_id, author_id="reply-author", author="Reply author", parent_id=reply["id"])
        recipients = feedback_comments.recipients(db, nested)
    finally:
        db.close()

    assert nested["parent_id"] == root["id"]
    assert recipients == ("owner", "root-author")


def test_migrated_legacy_reply_is_reported_as_new_on_its_root_thread(isolated_db):
    db = get_db()
    try:
        artifact_id, root, reply = _thread(db)
        legacy, _ = add_comment(db, artifact_id, author_id="owner", author="Owner", parent_id=root["id"])
        db.execute("UPDATE doc_comments SET parent_id=? WHERE id=?", (reply["id"], legacy["id"]))
        migrate_141_to_142(db)
        migrated = comments.get_comment(legacy["id"], db=db)
        threads = comments.list_for_artifact(artifact_id, db=db)
        feedback_state.annotate_thread_attention(db, "reader-user", artifact_id, threads)
    finally:
        db.close()

    assert migrated is not None and migrated["parent_id"] == root["id"]
    assert threads[0]["new_reply_ids"] == [legacy["id"]]
