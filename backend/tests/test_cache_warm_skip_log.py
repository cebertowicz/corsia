import asyncio
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from api import thread_cache_warm


@pytest.mark.parametrize("reason", [
    "no_candidate", "agent_has_model", "first_run_not_pending", "warm_failed",
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
