"""Schema 101 -> 102: index review requests by reviewer for the reviewer digest.

Adds the ``doc_review_requests(reviewer_thread_id, created_at)`` index the
digest reads. Runs inside the runner's transaction; it never commits.
"""

from __future__ import annotations

import sqlite3

from infra.doc_review_schema import DOC_REVIEW_REVIEWER_INDEX_SQL


def migrate_101_to_102(conn: sqlite3.Connection) -> None:
    conn.execute(DOC_REVIEW_REVIEWER_INDEX_SQL)
