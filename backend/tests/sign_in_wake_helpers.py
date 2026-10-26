"""Read the sign-in results delivered to agent threads, for the Google and Microsoft wake tests."""

from __future__ import annotations

import json

from infra.database import get_db

EVENT = "account.sign_in_finished"


def wakes() -> list[dict]:
    """Every sign-in result any thread was woken with: the facts plus the thread and chip, oldest first."""
    db = get_db()
    try:
        rows = db.execute(
            "SELECT thread_id, content, metadata FROM messages WHERE id LIKE 'sign-in:%' ORDER BY rowid",
        ).fetchall()
    finally:
        db.close()
    found = []
    for row in rows:
        head, _, details = row["content"].partition("Details:\n")
        assert head.startswith(f"[EVENT: {EVENT}]"), head
        found.append({
            "thread_id": row["thread_id"], "content": row["content"], "chip": json.loads(row["metadata"])["summary"],
            **json.loads(details),
        })
    return found


def registry_rows() -> tuple[int, int]:
    """How many artifact identities and registrations exist: a sign-in must leave both as they were."""
    db = get_db()
    try:
        return tuple(  # type: ignore[return-value]
            db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("artifact_identities", "artifact_registrations")
        )
    finally:
        db.close()


def no_account_artifact() -> bool:
    """True when no registry identity names a sign-in, the row class the old wake wrote."""
    db = get_db()
    try:
        return db.execute("SELECT 1 FROM artifact_identities WHERE integration='account'").fetchone() is None
    finally:
        db.close()
