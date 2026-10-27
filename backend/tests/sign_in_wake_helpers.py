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


def record_drives(monkeypatch) -> list[dict]:
    """Replace the thread drive with a recorder; each call notes what a different connection could see.

    The drive runs on the event loop and the writes on a pool thread, so what the recorder reads is
    only what has committed: ``accounts`` connected accounts and ``wakes`` queued sign-in wakes.
    """
    calls: list[dict] = []

    async def drive(thread_id: str, *_args, **_kwargs) -> None:
        db = get_db()
        calls.append({
            "thread": thread_id,
            "accounts": db.execute("SELECT COUNT(*) FROM connected_accounts WHERE state='connected'").fetchone()[0],
            "wakes": db.execute("SELECT COUNT(*) FROM messages WHERE id LIKE 'sign-in:%'").fetchone()[0],
        })

    monkeypatch.setattr("infra.thread_execution.drive_thread", drive)
    return calls


def refuse_sign_in_wakes(monkeypatch) -> None:
    """The thread refuses the sign-in wake and nothing else, so a report about it can still be queued."""
    from events import delivery
    from infra.send_types import SendRejected

    real = delivery.enqueue_event_wake

    def refusing(thread_id, internal_event, *args, **kwargs):
        if internal_event == EVENT:
            raise SendRejected(409, "held")
        return real(thread_id, internal_event, *args, **kwargs)

    monkeypatch.setattr("events.delivery.enqueue_event_wake", refusing)


def adopt(child: str, parent: str) -> None:
    """Make ``parent`` the owner a report about ``child`` goes to."""
    db = get_db()
    try:
        db.execute("UPDATE threads SET parent_thread_id=? WHERE id=?", (parent, child))
        db.commit()
    finally:
        db.close()


def reports_for(owner: str) -> list[str]:
    """The refused-wake reports queued on ``owner``."""
    db = get_db()
    try:
        return [
            row["id"] for row in db.execute(
                "SELECT id FROM messages WHERE thread_id=? AND id LIKE 'wake-failed:sign-in:%'", (owner,),
            ).fetchall()
        ]
    finally:
        db.close()


def parked_reports() -> list[dict]:
    """The refused-wake reports no live owner took."""
    from infra.failed_wake_surface import WAKE_FAILED_REASON

    db = get_db()
    try:
        return [
            json.loads(row["payload_json"]) for row in db.execute(
                "SELECT payload_json FROM dead_letter_events WHERE reason=?", (WAKE_FAILED_REASON,),
            ).fetchall()
        ]
    finally:
        db.close()
