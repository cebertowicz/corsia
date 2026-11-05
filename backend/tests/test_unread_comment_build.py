"""Regression tests for unread document comments."""

from doc_feedback_helpers import add_comment, seed_document, seed_thread
from doceditor.feedback_state import annotate_thread_attention, mark_thread_viewed
from doceditor.reply_attention import unread_items
from infra.database import get_db, write_transaction
from infra.database_migrations import migrate_to_current


def test_resolved_thread_reply_stays_unread_until_opened(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact = seed_document(db, "owner")
        root, _ = add_comment(db, artifact, author_kind="user", author_id=None)
        reply, _ = add_comment(db, artifact, author_id="owner", parent_id=root["id"])
        db.execute("UPDATE doc_comments SET resolved=1 WHERE id=?", (root["id"],))
        db.commit()
        threads = [{"id": root["id"]}]
        annotate_thread_attention(db, "owner-token", artifact, threads)
        assert threads[0]["new_reply_ids"] == [reply["id"]]
        with write_transaction(db, scope="test_unread_resolved_view") as connection:
            mark_thread_viewed(connection, "owner-token", artifact, root["id"], reply["id"])
        annotate_thread_attention(db, "owner-token", artifact, threads)
        assert threads[0]["new_reply_ids"] == []
    finally:
        db.close()


def test_new_agent_comment_requires_prior_person_comment_and_clears_on_open(isolated_db, tmp_path):
    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact = seed_document(db, "owner")
        path = tmp_path / "document.md"
        path.write_text("A document", encoding="utf-8")
        db.execute("UPDATE artifact_identities SET path=? WHERE id=?", (str(path), artifact))
        db.commit()
        old, _ = add_comment(db, artifact, author_id="owner")
        person, _ = add_comment(db, artifact, author_kind="user", author_id=None)
        new, _ = add_comment(db, artifact, author_id="owner")
        suggestion, _ = add_comment(db, artifact, author_id="owner", replacement="replacement")
        for index, row in enumerate((old, person, new, suggestion)):
            db.execute("UPDATE doc_comments SET created_at=? WHERE id=?", (f"2026-01-0{index + 1}T00:00:00Z", row["id"]))
        db.commit()
        threads = [{"id": row["id"]} for row in (old, new, suggestion)]
        annotate_thread_attention(db, "owner-token", artifact, threads)
        assert [row["new_comment"] for row in threads] == [False, True, False]
        items = unread_items(db, "owner-token")
        assert items[0]["detail"]["new_comment_count"] == 1
        with write_transaction(db, scope="test_unread_top_level_view") as connection:
            mark_thread_viewed(connection, "owner-token", artifact, new["id"], new["id"])
        annotate_thread_attention(db, "owner-token", artifact, threads)
        assert threads[1]["new_comment"] is False
        assert unread_items(db, "owner-token") == []
    finally:
        db.close()


def test_top_level_unread_ids_stay_reply_only_after_view(isolated_db):
    db = get_db()
    try:
        seed_thread(db, 'owner')
        artifact = seed_document(db, 'owner')
        person, _ = add_comment(db, artifact, author_kind='user', author_id=None)
        root, _ = add_comment(db, artifact, author_id='owner')
        touch, _ = add_comment(db, artifact, author_kind='user', author_id=None, parent_id=root['id'])
        reply, _ = add_comment(db, artifact, author_id='owner', parent_id=root['id'])
        for index, row in enumerate((person, root, touch, reply)):
            db.execute('UPDATE doc_comments SET created_at=? WHERE id=?', (f'2026-01-0{index + 1}T00:00:00Z', row['id']))
        db.commit()
        threads = [{'id': root['id']}]
        annotate_thread_attention(db, 'owner-token', artifact, threads)
        assert threads[0]['new_comment'] is True
        assert threads[0]['new_reply_ids'] == [reply['id']]
        with write_transaction(db, scope='test_top_level_view_ids') as connection:
            remaining = mark_thread_viewed(connection, 'owner-token', artifact, root['id'], root['id'])
        annotate_thread_attention(db, 'owner-token', artifact, threads)
        assert threads[0]['new_comment'] is False
        assert threads[0]['new_reply_ids'] == remaining == [reply['id']]
    finally:
        db.close()


def test_upgrade_stamps_newly_eligible_history_without_clearing_open_replies(isolated_db):
    db = get_db()
    try:
        seed_thread(db, "owner")
        artifact = seed_document(db, "owner")
        person, _ = add_comment(db, artifact, author_kind="user", author_id=None)
        root, _ = add_comment(db, artifact, author_id="owner")
        resolved, _ = add_comment(db, artifact, author_kind="user", author_id=None)
        old_reply, _ = add_comment(db, artifact, author_id="owner", parent_id=resolved["id"])
        open_reply, _ = add_comment(db, artifact, author_id="owner", parent_id=person["id"])
        db.execute("UPDATE doc_comments SET resolved=1 WHERE id=?", (resolved["id"],))
        db.execute("INSERT INTO users(id,password_hash,created_at,password_changed_at) VALUES (?,?,?,?)", ("second-person", "test", "2026-01-01", "2026-01-01"))
        db.execute("INSERT INTO doc_comment_thread_views(user_id,root_id,seen_at,seen_reply_id) VALUES (?,?,?,?)", ("second-person", resolved["id"], "2027-01-01T00:00:00Z", old_reply["id"]))
        for index, row in enumerate((person, root, resolved, old_reply, open_reply)):
            db.execute("UPDATE doc_comments SET created_at=? WHERE id=?", (f"2026-01-0{index + 1}T00:00:00Z", row["id"]))
        db.commit()
        db.execute("PRAGMA user_version=175")
        migrate_to_current(db, 176)
        first = [tuple(row) for row in db.execute("SELECT user_id,root_id,seen_at,seen_reply_id FROM doc_comment_thread_views ORDER BY user_id,root_id")]
        people = {row[0] for row in db.execute("SELECT id FROM users UNION SELECT user_id FROM threads WHERE user_id IS NOT NULL")}
        assert len(first) == 2 * len(people)  # One newer stamp was preserved; all other rows were inserted.
        assert len([row for row in first if row[0] == "owner-token"]) == 2
        assert len([row for row in first if row[0] == "second-person"]) == 2
        assert next(row for row in first if row[0] == "second-person" and row[1] == resolved["id"])[2] == "2027-01-01T00:00:00Z"
        rows = [{"id": item["id"]} for item in (root, resolved, person)]
        annotate_thread_attention(db, "owner-token", artifact, rows)
        assert rows[0]["new_comment"] is False
        assert rows[1]["new_reply_ids"] == []
        assert rows[2]["new_reply_ids"] == [open_reply["id"]]
        later, _ = add_comment(db, artifact, author_id="owner")
        migrate_to_current(db, 176)
        assert [tuple(row) for row in db.execute("SELECT user_id,root_id,seen_at,seen_reply_id FROM doc_comment_thread_views ORDER BY user_id,root_id")] == first
        fresh = [{"id": later["id"]}]
        annotate_thread_attention(db, "owner-token", artifact, fresh)
        assert fresh[0]["new_comment"] is True
    finally:
        db.close()
