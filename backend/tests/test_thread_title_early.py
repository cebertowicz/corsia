"""Tests for early thread title generation — Bug #186.

Validates that title generation fires immediately after the first message is
accepted/persisted, not after turn completion, and that explicitly-set or
spawn-supplied titles are never overwritten.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

os.environ.setdefault("CORSIA_WORKSPACE", "/tmp/corsia-test-title-early")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_session(thread_id: str, *, title: str = "New Chat", user_id: str = "user-1", model: str | None = "oMLX/qwen") -> None:
    from infra.database import get_db
    now = datetime.now(timezone.utc).isoformat()
    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id, user_id, title, model, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (thread_id, user_id, title, model, now, now),
        )
        db.commit()
    finally:
        db.close()


def _insert_message(thread_id: str, role: str, content: str) -> None:
    from infra.database import get_db
    now = datetime.now(timezone.utc).isoformat()
    msg_id = str(uuid.uuid4())
    db = get_db()
    try:
        db.execute(
            "INSERT INTO messages (id, thread_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (msg_id, thread_id, role, content, now),
        )
        db.commit()
    finally:
        db.close()


def _get_title(thread_id: str) -> str | None:
    from infra.database import get_db
    db = get_db()
    try:
        row = db.execute("SELECT title FROM threads WHERE id = ?", (thread_id,)).fetchone()
        return row["title"] if row else None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# start_title_early — unit tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_title_early_generates_for_new_chat(isolated_db):
    """Early trigger generates and persists a title for a 'New Chat' session."""
    _insert_session("s-early-1", title="New Chat")
    _insert_message("s-early-1", "user", "Help me debug the login flow")

    broadcasts: list[dict] = []
    from infra import state
    from ws.post_turn import start_title_early

    with patch("core.thread_naming._call_title_llm", return_value="Login Flow Debug"):
        with patch.object(state, "broadcast_event", side_effect=lambda sid, uid, msg: broadcasts.append(msg)):
            await start_title_early("s-early-1", "user-1", "Help me debug the login flow")

    assert _get_title("s-early-1") == "Login Flow Debug"
    updated = [m for m in broadcasts if m.get("type") == "session_updated"]
    assert len(updated) == 1
    assert updated[0]["title"] == "Login Flow Debug"
    assert updated[0]["thread_id"] == "s-early-1"


@pytest.mark.asyncio
async def test_start_title_early_skips_explicit_title(isolated_db):
    """Early trigger does not overwrite an already-set thread title."""
    _insert_session("s-early-2", title="Existing Custom Title")
    _insert_message("s-early-2", "user", "Some follow-up message")

    broadcasts: list[dict] = []
    from infra import state
    from ws.post_turn import start_title_early

    with patch("core.thread_naming._call_title_llm", return_value="Should Not Be Used") as mock_llm:
        with patch.object(state, "broadcast_event", side_effect=lambda sid, uid, msg: broadcasts.append(msg)):
            await start_title_early("s-early-2", "user-1", "Some follow-up message")

    assert _get_title("s-early-2") == "Existing Custom Title"
    mock_llm.assert_not_called()
    assert not any(m.get("type") == "session_updated" for m in broadcasts)


@pytest.mark.asyncio
async def test_start_title_early_skips_spawn_title(isolated_db):
    """Spawn-created threads with non-placeholder titles are not overwritten."""
    _insert_session("s-spawn-1", title="Feature Impl Worker")
    _insert_message("s-spawn-1", "user", "Implement the auth module")

    from infra import state
    from ws.post_turn import start_title_early

    with patch("core.thread_naming._call_title_llm", return_value="Override") as mock_llm:
        with patch.object(state, "broadcast_event", lambda *a, **k: None):
            await start_title_early("s-spawn-1", "user-1", "Implement the auth module")

    assert _get_title("s-spawn-1") == "Feature Impl Worker"
    mock_llm.assert_not_called()


@pytest.mark.asyncio
async def test_no_duplicate_save_when_post_turn_fires_after_early(isolated_db):
    """Post-turn ensure_thread_title skips the DB write when early trigger already set the title."""
    _insert_session("s-nodup-1", title="New Chat")
    _insert_message("s-nodup-1", "user", "Fix the auth bug")

    from infra import state
    from ws.post_turn import start_title_early, ensure_thread_title

    llm_calls: list[str] = []

    async def _counted_llm(prompt: str, _model: str, *, thread_id: str | None = None) -> str:
        llm_calls.append(prompt)
        return "Auth Bug Fix"

    # Simulate early trigger
    with patch("core.thread_naming._call_title_llm", side_effect=_counted_llm):
        with patch.object(state, "broadcast_event", lambda *a, **k: None):
            await start_title_early("s-nodup-1", "user-1", "Fix the auth bug")

    assert _get_title("s-nodup-1") == "Auth Bug Fix"
    assert len(llm_calls) == 1  # LLM called exactly once by the early trigger

    # Simulate post-turn firing the same ensure_thread_title
    second_sends: list[dict] = []

    async def _ws_send(msg: dict) -> None:
        second_sends.append(msg)

    with patch("core.thread_naming._call_title_llm", side_effect=_counted_llm):
        await ensure_thread_title("s-nodup-1", "Fix the auth bug", _ws_send)

    # LLM must NOT have been called again; title unchanged; no second broadcast
    assert len(llm_calls) == 1
    assert _get_title("s-nodup-1") == "Auth Bug Fix"
    assert not second_sends


@pytest.mark.asyncio
async def test_start_title_early_fallback_content_on_llm_failure(isolated_db):
    """Falls back to truncated first-message content when LLM returns None."""
    _insert_session("s-fallback-1", title="New Chat")
    _insert_message("s-fallback-1", "user", "A" * 80)

    from infra import state
    from ws.post_turn import start_title_early

    with patch("core.thread_naming._call_title_llm", return_value=None):
        with patch.object(state, "broadcast_event", lambda *a, **k: None):
            await start_title_early("s-fallback-1", "user-1", "A" * 80)

    title = _get_title("s-fallback-1")
    # Should be the first 50 chars + "..."
    assert title is not None
    assert title != "New Chat"
    assert len(title) <= 53  # 50 + "..."


# ---------------------------------------------------------------------------
# Integration: early trigger fires from native WS message_dispatch path
# ---------------------------------------------------------------------------

async def _run_in_db_sync(fn, *a, **k):
    """Test stand-in for run_in_db: runs the function directly in the event loop."""
    return fn(*a, **k)


@pytest.mark.asyncio
async def test_native_ws_defers_new_chat_title_until_send_acceptance(
    isolated_db, monkeypatch
):
    # Use model=None so the native dispatch safety net (is_dispatchable_model) is skipped
    _insert_session("ws-native-1", title="New Chat", user_id="user-n", model=None)
    _insert_message("ws-native-1", "user", "Help me build a REST API")

    from ws.message_dispatch import handle_message
    import ws.post_turn as post_turn_mod

    early_calls: list[tuple] = []

    async def _fake_early(thread_id, user_id, fallback_content):
        early_calls.append((thread_id, user_id, fallback_content))

    monkeypatch.setattr(post_turn_mod, "start_title_early", _fake_early)

    class _FakeWS:
        async def send_json(self, _):
            pass

    fake_ws = _FakeWS()
    # Use a plain dict so handle_message's session["reasoning_effort"] access
    # doesn't depend on the migration columns missing from isolated_db.
    session = {
        "id": "ws-native-1", "title": "New Chat", "model": None,
        "user_id": "user-n", "reasoning_effort": None,
        "project_id": None, "spawned_by": None, "parent_session_id": None,
    }

    send_kwargs = {}

    async def _fake_send_thread_input(thread_id, sess, content, **kwargs):
        send_kwargs.update(kwargs)
        after = kwargs.get("after_persist_before_dispatch")
        if after:
            await after(kwargs.get("target"))

        class _Result:
            driver = "turn_engine"
            message_id = "msg-1"
            created_at = "2026-07-14T12:00:00+00:00"
            turn_id = "turn-title"
            turn_order = 1
            turn_task = None
            duplicate = False
        return _Result()

    from infra.database import get_db as _real_get_db

    await handle_message(
        fake_ws,
        {"id": "user-n"},
        "user-n",
        {
            "type": "message",
            "thread_id": "ws-native-1",
            "input_id": "input-title",
            "content": "Help me build a REST API",
        },
        get_db_fn=_real_get_db,
        run_in_db_fn=_run_in_db_sync,
        get_thread_for_user_fn=lambda *a, **k: session,
        touch_thread_fn=lambda *a, **k: None,
        send_thread_input_fn=_fake_send_thread_input,
        handle_cc_main_message_fn=AsyncMock(),
        handle_codex_main_message_fn=AsyncMock(),
    )

    await asyncio.sleep(0)  # let tasks run

    assert early_calls == []
    assert send_kwargs["accepted_effects"]["title"] is True


@pytest.mark.asyncio
async def test_native_ws_skips_early_title_for_retry(isolated_db, monkeypatch):
    """handle_message does NOT fire start_title_early for retries."""
    _insert_session("ws-retry-1", title="New Chat", user_id="user-r", model=None)

    from ws.message_dispatch import handle_message
    import ws.post_turn as post_turn_mod

    early_calls: list = []

    async def _fake_early(thread_id, user_id, fallback_content):
        early_calls.append(thread_id)

    monkeypatch.setattr(post_turn_mod, "start_title_early", _fake_early)

    class _FakeWS:
        async def send_json(self, _):
            pass

    fake_ws = _FakeWS()
    session = {
        "id": "ws-retry-1", "title": "New Chat", "model": None,
        "user_id": "user-r", "reasoning_effort": None,
        "project_id": None, "spawned_by": None, "parent_session_id": None,
    }

    send_kwargs = {}

    async def _fake_send_thread_input(thread_id, sess, content, **kwargs):
        send_kwargs.update(kwargs)
        after = kwargs.get("after_persist_before_dispatch")
        if after:
            await after(kwargs.get("target"))

        class _Result:
            driver = "turn_engine"
            message_id = None
            created_at = None
            turn_id = "turn-retry"
            turn_order = 2
            turn_task = None
            duplicate = False
        return _Result()

    from infra.database import get_db as _real_get_db

    await handle_message(
        fake_ws,
        {"id": "user-r"},
        "user-r",
        {
            "type": "message",
            "thread_id": "ws-retry-1",
            "input_id": "input-retry",
            "content": "retry content",
            "is_retry": True,
        },
        get_db_fn=_real_get_db,
        run_in_db_fn=_run_in_db_sync,
        get_thread_for_user_fn=lambda *a, **k: session,
        touch_thread_fn=lambda *a, **k: None,
        send_thread_input_fn=_fake_send_thread_input,
        handle_cc_main_message_fn=AsyncMock(),
        handle_codex_main_message_fn=AsyncMock(),
    )

    await asyncio.sleep(0)
    assert early_calls == []
    assert send_kwargs["accepted_effects"]["title"] is False


@pytest.mark.asyncio
async def test_cc_defers_new_chat_title_until_send_acceptance(
    isolated_db, monkeypatch
):
    _insert_session("cc-1", title="New Chat", user_id="user-cc")

    from ws.message_dispatch import handle_cc_main_message
    import ws.post_turn as post_turn_mod

    early_calls: list[tuple] = []

    async def _fake_early(thread_id, user_id, fallback_content):
        early_calls.append((thread_id, user_id, fallback_content))

    monkeypatch.setattr(post_turn_mod, "start_title_early", _fake_early)

    class _FakeWS:
        async def send_json(self, _):
            pass

    session = {
        "id": "cc-1", "title": "New Chat", "user_id": "user-cc",
        "model": "claude-code/sonnet", "reasoning_effort": None,
        "project_id": None, "spawned_by": None, "parent_session_id": None,
    }

    send_kwargs = {}

    async def _fake_send(*args, **kwargs):
        send_kwargs.update(kwargs)
        class _Result:
            driver = "cc_runtime"
            message_id = "msg-cc"
            turn_task = None
            duplicate = False
        return _Result()

    await handle_cc_main_message(
        _FakeWS(),
        session,
        "cc-1",
        "build a feature",
        [],
        msg_id="",
        now="",
        is_retry=False,
        is_edited=False,
        send_thread_input_fn=_fake_send,
        cc_attachment_block_fn=lambda *_: "",
    )

    await asyncio.sleep(0)
    assert early_calls == []
    assert send_kwargs["accepted_effects"]["title"] is True


@pytest.mark.asyncio
async def test_cc_no_early_title_on_send_failure(isolated_db, monkeypatch):
    """handle_cc_main_message does NOT fire start_title_early when send fails."""
    _insert_session("cc-fail-1", title="New Chat", user_id="user-cf")

    from ws.message_dispatch import handle_cc_main_message
    from infra.send_service import SendRejected
    import ws.post_turn as post_turn_mod

    early_calls: list = []

    async def _fake_early(thread_id, user_id, fallback_content):
        early_calls.append(thread_id)

    monkeypatch.setattr(post_turn_mod, "start_title_early", _fake_early)

    class _FakeWS:
        sent: list = []
        async def send_json(self, msg):
            self.sent.append(msg)

    session = {
        "id": "cc-fail-1", "title": "New Chat", "user_id": "user-cf",
        "model": "claude-code/sonnet", "reasoning_effort": None,
        "project_id": None, "spawned_by": None, "parent_session_id": None,
    }
    fake_ws = _FakeWS()

    async def _reject(*args, **kwargs):
        raise SendRejected(409, "turn already active")

    await handle_cc_main_message(
        fake_ws,
        session,
        "cc-fail-1",
        "some content",
        [],
        msg_id="",
        now="",
        is_retry=False,
        is_edited=False,
        send_thread_input_fn=_reject,
        cc_attachment_block_fn=lambda *_: "",
    )

    await asyncio.sleep(0)
    assert early_calls == []
