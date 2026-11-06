"""Schema 179 -> 180: index document comment replies by their thread, and record which threads a decision closed.

Deciding a suggestion reads whether it has replies, and reply routing reads a
thread's replies (``doceditor.comment_recipients``); this index bounds each
read to that thread instead of every comment ever written.

``closed_by_decision`` lets the first reply re-open a suggestion its decision
closed (``comments.create_with_status``). Before this step every decision
resolved its suggestion, and Re-open never left a decided suggestion resolved
(it clears a rejection, and refuses an accepted one), so a decided, resolved
suggestion here is exactly one its decision closed.

Runs inside the runner's transaction; it never commits or rolls back.
"""

from __future__ import annotations

import sqlite3

from infra.doc_comments_schema import REPLIES_BY_PARENT_INDEX_SQL


def migrate_179_to_180(conn: sqlite3.Connection) -> None:
    """Add the replies-by-parent index and the decision-closed mark where the comments table has them."""
    columns = {column[1] for column in conn.execute("PRAGMA table_info(doc_comments)")}
    if "parent_id" in columns:
        conn.execute(REPLIES_BY_PARENT_INDEX_SQL)
    if "decision" not in columns:
        return
    if "closed_by_decision" not in columns:
        conn.execute("ALTER TABLE doc_comments ADD COLUMN closed_by_decision INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            "UPDATE doc_comments SET closed_by_decision=1 WHERE decision IS NOT NULL AND resolved=1"
            + (" AND parent_id IS NULL" if "parent_id" in columns else "")
        )
