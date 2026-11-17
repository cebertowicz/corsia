"""A user-driven Claude Code child's close notice is written off the event loop (#2913).

Handle finalization is sync code on the event loop. Its close notice to the
parent used to open a SQLite write transaction right there, where
``BEGIN IMMEDIATE`` waits on every other writer and freezes every request.
With ``CORSIA_DB_LOOP_WRITE_STRICT=1`` that write raises instead of only
reporting, so this test drives the real close path and fails loudly if the
write moves back onto the loop.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from infra.db_loop_guard import LOOP_WRITE_STRICT_ENV


def _seed_parent() -> None:
    from infra.database import get_db

    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,title,created_at,updated_at) "
        "VALUES ('close-parent','u-test','Parent','2026-01-01','2026-01-01')"
    )
    db.commit()
    db.close()


@pytest.mark.asyncio
async def test_close_notice_is_written_off_the_event_loop(isolated_db, monkeypatch):
    from infra.database import get_db
    from subagents.cc_handle_close import emit_logical_close
    from human_usage_fixtures import drain_background

    _seed_parent()
    events: list[tuple[str, dict]] = []
    live = SimpleNamespace(parent_thread_id="close-parent", user_driving=True, name="Worker")
    monkeypatch.setenv(LOOP_WRITE_STRICT_ENV, "1")

    emit_logical_close("close-child", live, lambda thread, payload: events.append((thread, payload)))
    await drain_background()

    db = get_db()
    try:
        rows = db.execute(
            "SELECT content, metadata FROM messages WHERE thread_id='close-parent'"
        ).fetchall()
    finally:
        db.close()
    assert [row["content"] for row in rows] == ["Subagent “Worker” closed."]
    kinds = [(thread, payload["type"]) for thread, payload in events]
    assert kinds == [
        ("close-child", "subagent_closed"),
        ("close-parent", "subagent_status"),
        ("close-parent", "message_added"),
    ]
