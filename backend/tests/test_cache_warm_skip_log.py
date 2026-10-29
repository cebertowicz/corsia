import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from api import thread_cache_warm


@pytest.mark.parametrize("reason", [
    "no_candidate", "agent_has_model", "first_run_not_pending",
])
def test_skip_reason_logs_once_without_warming(monkeypatch, reason):
    monkeypatch.setattr(thread_cache_warm, "authenticate", lambda _: {"id": "user"})
    load = Mock(side_effect=AssertionError("must not load a model"))
    monkeypatch.setattr(thread_cache_warm, "_load_warm_target", load)
    lines = []
    sink = thread_cache_warm.logger.add(lambda msg: lines.append(msg.record["message"]))
    try:
        result = asyncio.run(thread_cache_warm.warm_session_cache("thread", token="secret", skip_reason=reason))
    finally:
        thread_cache_warm.logger.remove(sink)
    assert result == {"ok": True}
    assert lines == [f"warm_session_cache skipped: {reason}"]
    print(lines[0])
    load.assert_not_called()


@pytest.mark.parametrize("stage, expected", [
    ("target", "target resolution failed"),
    ("proof", "target validation failed"),
    ("prefix", "prefix construction failed"),
    ("provider", "model not loaded"),
])
def test_warm_failure_logs_one_safe_server_reason(monkeypatch, stage, expected):
    monkeypatch.setattr(thread_cache_warm, "authenticate", lambda _: {"id": "user"})
    monkeypatch.setattr(thread_cache_warm, "_load_warm_target", Mock(return_value=("target", None, None, {})))
    monkeypatch.setattr(thread_cache_warm, "_prove_warm_target", AsyncMock(return_value="target"))
    monkeypatch.setattr(thread_cache_warm, "_build_warm_prefix", AsyncMock(return_value=([], [], {})))
    monkeypatch.setattr(thread_cache_warm, "_submit_warm", AsyncMock(return_value={"ok": False, "message": "model not loaded: private"}))
    if stage == "target":
        monkeypatch.setattr(thread_cache_warm, "_load_warm_target", Mock(return_value={"ok": False, "message": "private"}))
    elif stage == "proof":
        monkeypatch.setattr(thread_cache_warm, "_prove_warm_target", AsyncMock(return_value={"ok": False, "message": "private"}))
    elif stage == "prefix":
        monkeypatch.setattr(thread_cache_warm, "_build_warm_prefix", AsyncMock(side_effect=ValueError("private")))
    lines = []
    sink = thread_cache_warm.logger.add(lambda msg: lines.append(msg.record["message"]))
    try:
        if stage == "prefix":
            with pytest.raises(ValueError):
                asyncio.run(thread_cache_warm.warm_session_cache("thread", token="private", wait=True))
        else:
            result = asyncio.run(thread_cache_warm.warm_session_cache("thread", token="private", wait=True))
            assert result["ok"] is False
    finally:
        thread_cache_warm.logger.remove(sink)
    assert lines == [f"warm_session_cache skipped: warm_failed: {expected}"]


def test_invalid_skip_reason_is_rejected_without_logging(monkeypatch):
    monkeypatch.setattr(thread_cache_warm, "authenticate", lambda _: {"id": "user"})
    lines = []
    sink = thread_cache_warm.logger.add(lambda msg: lines.append(msg.record["message"]))
    try:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(thread_cache_warm.warm_session_cache("thread", token="secret", skip_reason="free text"))
    finally:
        thread_cache_warm.logger.remove(sink)
    assert exc.value.status_code == 400
    assert lines == []
