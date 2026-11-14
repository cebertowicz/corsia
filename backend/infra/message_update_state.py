"""Canonical update provenance, links and all-or-nothing replacement effects."""
from infra.message_update_seen import seen
from tools.post_update_text import seen_refusal


def register(db, message_id, thread_id, kind, node_id=None):
    """Mark a real tool update or reminder fallback, preserving original seen evidence."""
    db.execute("INSERT OR IGNORE INTO message_update_state "
               "(message_id,thread_id,posted_at,presence_at_post,seen_at,seen_reason) "
               "SELECT id,thread_id,created_at,'unknown',created_at,'unknown' FROM messages WHERE id=?",
               (message_id,))
    db.execute("UPDATE message_update_state SET update_kind=? WHERE message_id=? AND thread_id=?",
               (kind, message_id, thread_id))
    if node_id:
        db.execute("INSERT OR IGNORE INTO message_update_nodes VALUES (?,?)", (message_id, node_id))


def current(db, message_id):
    """Resolve a replaced message to its current descendant without changing message numbers."""
    row = db.execute("WITH RECURSIVE chain(id) AS (VALUES (?) UNION ALL "
                     "SELECT s.replaced_by FROM message_update_state s JOIN chain c ON s.message_id=c.id "
                     "WHERE s.replaced_by IS NOT NULL) SELECT m.id,m.seq FROM chain c JOIN messages m ON m.id=c.id "
                     "LEFT JOIN message_update_state s ON s.message_id=m.id WHERE s.replaced_by IS NULL",
                     (message_id,)).fetchone()
    return row


def validate_targets(db, thread_id, user_id, ids, live, now):
    """Refuse the whole call if any named row is invalid or seen."""
    for message_id in ids:
        row = db.execute("SELECT m.role,m.thread_id AS owning_thread,s.* FROM messages m "
                         "LEFT JOIN message_update_state s ON s.message_id=m.id WHERE m.id=?",
                         (message_id,)).fetchone()
        if row is None:
            raise ValueError(f"{message_id} is not a known message. Use get_thread_history to recover the update's id; "
                             "do not guess. A new subject needs no replace.")
        if row["owning_thread"] != thread_id:
            raise ValueError(f"{message_id} is a message in another chat; replace only your own updates in this chat.")
        if row["role"] == "user":
            raise ValueError(f"{message_id} is the person's message; replace only your own unseen updates.")
        if row["update_kind"] is None:
            raise ValueError(f"{message_id} is an ordinary reply, not an update; replace only your own unseen updates.")
        if row["replaced_by"]:
            target = current(db, message_id)
            raise ValueError(f"{message_id} was already replaced by {target['id']} (message #{target['seq']}). "
                             "Use the current update if it is still unseen.")
        verdict = seen(db, row, user_id, live, now)
        if verdict.reason:
            raise ValueError(seen_refusal(message_id, verdict))


def apply_replacement(db, thread_id, message_id, ids, reason, now):
    """Retain old rows and answers, transferring only current update/task projections."""
    for old in ids:
        db.execute("UPDATE message_update_state SET replaced_by=?,replaced_at=?,replace_reason=? WHERE message_id=?",
                   (message_id, now, reason, old))
        db.execute("INSERT OR IGNORE INTO message_update_nodes SELECT ?,node_id "
                   "FROM message_update_nodes WHERE message_id=?", (message_id, old))
        db.execute("DELETE FROM message_update_nodes WHERE message_id=?", (old,))
        db.execute("DELETE FROM thread_updates WHERE thread_id=? AND kind='marked' "
                   "AND source_kind='message' AND source_id=?", (thread_id, old))


def unseen_updates(db, thread_id, user_id, exclude_id, live, now):
    """Return every other active unseen update, newest first, with its visible chat text."""
    rows = db.execute("SELECT s.*,m.seq,COALESCE(cv.content,m.content) AS content "
                      "FROM message_update_state s JOIN messages m ON m.id=s.message_id "
                      "LEFT JOIN chat_visible_content cv ON cv.message_id=m.id "
                      "WHERE s.thread_id=? AND s.update_kind IS NOT NULL AND s.replaced_by IS NULL "
                      "AND s.seen_at IS NULL AND s.message_id!=? ORDER BY m.seq DESC",
                      (thread_id, exclude_id)).fetchall()
    return [row for row in rows if not seen(db, row, user_id, live, now).reason]


def projection_fields(db, message_id):
    """Public removal ids and task links, never archived text or private evidence."""
    ids = [row[0] for row in db.execute(
        "WITH RECURSIVE ancestors(id) AS (SELECT message_id FROM message_update_state WHERE replaced_by=? "
        "UNION ALL SELECT s.message_id FROM message_update_state s JOIN ancestors a ON s.replaced_by=a.id) "
        "SELECT id FROM ancestors", (message_id,))]
    nodes = [row[0] for row in db.execute("SELECT node_id FROM message_update_nodes WHERE message_id=? ORDER BY node_id",
                                        (message_id,))]
    return {**({"replaced_message_ids": ids} if ids else {}), **({"update_node_ids": nodes} if nodes else {})}


def delete_thread(db, thread_id):
    """Delete auxiliary state in the thread owner's existing deletion transaction."""
    db.execute("DELETE FROM message_update_nodes WHERE message_id IN "
               "(SELECT message_id FROM message_update_state WHERE thread_id=?)", (thread_id,))
    db.execute("DELETE FROM message_update_state WHERE thread_id=?", (thread_id,))
