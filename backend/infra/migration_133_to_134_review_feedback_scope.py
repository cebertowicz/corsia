"""Keep review-fork comment staging on its request, outside ordinary batches."""

from __future__ import annotations

import sqlite3

from infra.doc_review_schema import DOC_REVIEW_REQUEST_COMMENTS_SQL


def migrate_133_to_134(conn: sqlite3.Connection) -> None:
    """Add request-owned staging; ordinary batch identity is unchanged from v133."""
    conn.execute(DOC_REVIEW_REQUEST_COMMENTS_SQL)
