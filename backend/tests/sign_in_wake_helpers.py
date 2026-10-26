"""Read the sign-in results delivered to agent threads, for the Google and Microsoft wake tests."""

from __future__ import annotations

import json

from infra.database import get_db

EVENT = "account.sign_in_finished"


def wakes() -> list[dict]:
    """Every sign-in result any thread was woken with: the facts plus the thread, oldest first."""
    db = get_db()
    try:
        rows = db.execute(
            "SELECT thread_id, content FROM messages WHERE id LIKE 'platform-event:account:%' ORDER BY rowid",
        ).fetchall()
    finally:
        db.close()
    found = []
    for row in rows:
        head, _, details = row["content"].partition("Details:\n")
        assert head.startswith(f"[EVENT: {EVENT}]"), head
        found.append({"thread_id": row["thread_id"], "content": row["content"], **json.loads(details)})
    return found
