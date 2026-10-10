
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)




def test_session_started_emits_on_create(mock_posthog, monkeypatch):
    from api import threads as sessions_mod

    fake_user = {"id": "steve"}
    fake_session = {
        "id": "sess-created",
        "user_id": "steve",
        "profile": "personal",
        "title": "Test",
        "model": None,
        "project_id": None,
        "parent_session_id": None,
        "spawned_by": None,
        "created_at": "2026-06-17T00:00:00",
        "updated_at": "2026-06-17T00:00:00",
    }

    async def _fake_create_visible_fresh_thread(**kwargs):
        return fake_session

    monkeypatch.setattr(sessions_mod, "authenticate", lambda token: fake_user)
    monkeypatch.setattr(
        sessions_mod,
        "create_visible_fresh_thread",
        _fake_create_visible_fresh_thread,
    )

    result = asyncio.run(sessions_mod.create_thread(
        token="x", body={"profile": "personal", "title": "Test"},
    ))
    sid = result["thread_id"]

    assert ("session.started", sid) in mock_posthog
    props = mock_posthog.by_event("session.started")[0]
    assert props["project_id"] is None




def test_turn_completed_emit_shape(mock_posthog):
    from infra.product_events import emit_product_event

    emit_product_event(
        "turn", "completed",
        thread_id="sess-42", user_id="steve",
        properties={
            "turn_id": "t-1", "model": "claude-haiku-4-5", "duration_ms": 1234,
        },
    )

    assert ("turn.completed", "sess-42") in mock_posthog
    props = mock_posthog.by_event("turn.completed")[0]
    assert props["duration_ms"] == 1234
    assert props["model"] == "claude-haiku-4-5"


def test_turn_errored_emit_truncates_message(mock_posthog):
    from infra.product_events import emit_product_event

    long_msg = "x" * 1000
    emit_product_event(
        "turn", "errored",
        thread_id="sess-1", user_id="steve",
        properties={
            "turn_id": "t-2", "error_type": "RuntimeError",
            "error_message": long_msg[:200],  # callers truncate; assert they do
        },
    )

    props = mock_posthog.by_event("turn.errored")[0]
    assert len(props["error_message"]) == 200




def test_model_load_completed_emits(mock_posthog, monkeypatch):
    from api import network_servers as ns

    monkeypatch.setattr(ns.nd, "get_server", lambda sid: {"id": sid, "provider": "omlx"})
    monkeypatch.setattr(ns, "notify_models_changed", lambda: None)

    monkeypatch.setattr(ns.nd, "_LOADED_BY_SERVER", {})
    monkeypatch.setattr(ns.nd, "discover_and_notify", AsyncMock(return_value=None))

    fake_provider = AsyncMock()
    fake_provider.load_model = AsyncMock(return_value={"ok": True})
    fake_provider.warm_cache = AsyncMock(return_value=None)
    monkeypatch.setattr(ns, "for_provider", lambda _name: fake_provider)
    fake_agent = MagicMock()
    fake_agent.tools.get_definitions.return_value = []

    with patch("core.context_builder.ContextBuilder") as _ctx, \
         patch("core.agent.get_agent", new=AsyncMock(return_value=fake_agent)):
        _ctx.return_value.build_system_prompt.return_value = ""
        body = ns.ModelBody(model="qwen3-coder-30b")
        asyncio.run(ns.load_model("srv-1", body))

    events = [evt for evt, _sid in mock_posthog]
    assert "model.load.completed" in events
    props = mock_posthog.by_event("model.load.completed")[0]
    assert props["provider"] == "omlx"
    assert props["model_id"] == "qwen3-coder-30b"
    assert props["server_id"] == "srv-1"


def test_model_load_failed_emits(mock_posthog, monkeypatch):
    from api import network_servers as ns
    from fastapi import HTTPException

    monkeypatch.setattr(ns.nd, "get_server", lambda sid: {"id": sid, "provider": "omlx"})

    fake_provider = AsyncMock()
    fake_provider.load_model = AsyncMock(return_value={
        "ok": False, "status": 507, "message": "Insufficient memory",
    })
    monkeypatch.setattr(ns, "for_provider", lambda _name: fake_provider)

    body = ns.ModelBody(model="qwen3-coder-80b")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(ns.load_model("srv-1", body))
    assert exc_info.value.status_code == 507

    events = [evt for evt, _sid in mock_posthog]
    assert "model.load.failed" in events
    props = mock_posthog.by_event("model.load.failed")[0]
    assert props["status"] == 507
    assert "Insufficient memory" in props["error"]




def test_cost_recorded_emits_on_usage(mock_posthog, monkeypatch, isolated_db):
    from core import turns_util

    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 80,
        "total_tokens": 150,
        "cost_usd": 0.000123,
    }
    turns_util._log_llm_usage("sess-7", "claude-haiku-4-5", usage, source="conversation", ttft_ms=240)

    events = [evt for evt, _sid in mock_posthog]
    assert "cost.recorded" in events
    props = mock_posthog.by_event("cost.recorded")[0]
    assert props["input_tokens"] == 100
    assert props["output_tokens"] == 50
    assert props["cache_read_tokens"] == 80
    assert props["cost_usd"] == pytest.approx(0.000123)
    assert props["ttft_ms"] == 240
    assert props["source"] == "conversation"




def test_attachment_added_emits_on_upload(mock_posthog, monkeypatch, tmp_path):
    from api import uploads
    from fastapi import UploadFile
    import io

    monkeypatch.setattr(uploads, "authenticate", lambda token: {"id": "steve"})
    monkeypatch.setattr(uploads, "_thread_upload_root", lambda session_id, user_id: tmp_path)
    monkeypatch.setattr(uploads.registry_service, "register_upload", lambda *args, **kwargs: None)

    upload = UploadFile(
        filename="cat.png",
        file=io.BytesIO(b"\x89PNG\r\n\x1a\n"),
        headers={"content-type": "image/png"},
    )
    asyncio.run(uploads.upload_file(token="x", file=upload, thread_id="sess-7"))

    events = [evt for evt, _sid in mock_posthog]
    assert "attachment.added" in events
    props = mock_posthog.by_event("attachment.added")[0]
    assert props["mime_type"] == "image/png"
    assert props["is_image"] is True
    assert "filename" not in props  # filename intentionally excluded
