"""Schema 181 -> 182: an agent's ask for the person is a mark on the comment it belongs to.

A person tag was briefly kept as a ``doc_comment_tags`` row keyed by the person's principal. That
put a non-agent in ``agent_id``/``agent_name``, so every reader of that table - the locality rule,
leave, left-reply filtering, notification counting, and any later one - had to learn an unwritten
rule to tell a person apart from an agent, and the unread read needed a second table for a fact
about the row it was already reading.

``tags_person`` puts the mark on the comment itself, so ``doc_comment_tags`` means agent tags again
and the unread read is a column test. Any row the earlier shape left behind is carried to the new
mark first: that shape never reached a release, so at most a database that ran the review head has
one, and the person's tag stays visible either way.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

#: The principal a person tag was keyed by while it lived in doc_comment_tags (doceditor.feedback_state).
PERSON_TAG_PRINCIPAL = "ui"


def migrate_181_to_182(conn: sqlite3.Connection) -> None:
    """Add the person-tag mark, carry the old shape's rows onto it, and drop those rows."""
    columns = {column[1] for column in conn.execute("PRAGMA table_info(doc_comments)")}
    if not columns:
        # A database whose comments table has not arrived yet: it is created on demand from
        # the schema fragment (infra/doc_comments_schema.py), which already carries the column.
        return
    if "tags_person" not in columns:
        conn.execute("ALTER TABLE doc_comments ADD COLUMN tags_person INTEGER NOT NULL DEFAULT 0")
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "doc_comment_tags" not in tables:
        return
    conn.execute(
        "UPDATE doc_comments SET tags_person=1"
        " WHERE tags_person=0 AND id IN (SELECT comment_id FROM doc_comment_tags WHERE agent_id=?)",
        (PERSON_TAG_PRINCIPAL,),
    )
    conn.execute("DELETE FROM doc_comment_tags WHERE agent_id=?", (PERSON_TAG_PRINCIPAL,))
