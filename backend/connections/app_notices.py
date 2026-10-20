"""Passive connected-app state notices for threads reached by an app."""

import sqlite3

from connections.app_reach import AppReach, _reach, _reach_reaches_thread
from infra.database import write_transaction

def append_app_notice(
    app_id: str,
    summary: str,
    *,
    db: sqlite3.Connection,
    state: str | None = None,
    reach_before: AppReach | None = None,
    reach_after: AppReach | None = None,
    removed: bool = False,
) -> None:
    """Passively notify affected threads through the one canonical reach owner.

    Runtime changes notify every currently reachable thread. A reach transition
    notifies only a thread whose availability changed; that lets a project that
    remains on avoid an irrelevant notice while both sides of a removal hear it.
    The stable path retains only each app's latest state for one delivery.
    """
    before = reach_before
    after = None if removed else reach_after or _reach(app_id, db=db)
    from infra.passive_changes import append_thread_context_note

    details = {"path": f"connected_apps/{app_id}", "app_id": app_id}
    if state is not None:
        details["state"] = state
    for row in db.execute("SELECT id FROM threads"):
        thread_id = row["id"]
        reached_before = before is not None and _reach_reaches_thread(before, thread_id, db=db)
        reached_after = after is not None and _reach_reaches_thread(after, thread_id, db=db)
        if (before is None and reached_after) or (before is not None and reached_before != reached_after):
            append_thread_context_note(db, thread_id=thread_id, summary=summary, details=details)


def record_runtime_app_state(app_id: str, state: dict[str, object]) -> None:
    """Atomically journal one live runtime state to every reachable thread."""
    from infra.database import get_db

    db = get_db()
    try:
        with write_transaction(db, scope="connected_app_runtime_notice") as conn:
            row = conn.execute("SELECT name FROM connected_apps WHERE id=?", (app_id,)).fetchone()
            if row is None:
                return
            name = row["name"]
            app_state = state.get("state")
            tool_count = state.get("tool_count")
            if not isinstance(name, str) or not isinstance(app_state, str):
                return
            suffix = f" ({tool_count} actions)" if app_state == "working" and isinstance(tool_count, int) else ""
            append_app_notice(
                app_id,
                f"{name}: {app_state}{suffix}",
                db=conn,
                state=app_state,
            )
    finally:
        db.close()
