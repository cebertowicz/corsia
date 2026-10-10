import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from connections.auth_failure import SIGN_IN_EXPIRED, auth_failure_kind


def test_only_claudes_exact_unrefreshable_session_error_is_definitive() -> None:
    assert auth_failure_kind(
        "claude-code", "Failed to authenticate: OAuth session expired and could not be refreshed",
    ) == SIGN_IN_EXPIRED
    assert auth_failure_kind("claude-code", "HTTP 401 from proxy; retry the request") is None
    assert auth_failure_kind(
        "codex", "Failed to authenticate: OAuth session expired and could not be refreshed",
    ) is None


@pytest.mark.asyncio
async def test_claude_terminal_error_schedules_only_the_exact_expired_login_recheck(monkeypatch) -> None:
    from connections import claude_discovery
    from subagents.cc_runtime_errors import reconcile_expired_login

    reconcile = AsyncMock()
    monkeypatch.setattr(claude_discovery, "reconcile_turn_auth_failure", reconcile)
    logger = logging.getLogger(__name__)
    detail = "Failed to authenticate: OAuth session expired and could not be refreshed"

    reconcile_expired_login(detail, logger)
    reconcile_expired_login("HTTP 401 from proxy; retry the request", logger)
    await asyncio.sleep(0)

    reconcile.assert_awaited_once_with(detail)


def test_claude_terminal_error_without_a_running_loop_is_ignored(caplog) -> None:
    from subagents.cc_runtime_errors import reconcile_expired_login

    detail = "Failed to authenticate: OAuth session expired and could not be refreshed"
    reconcile_expired_login(detail, logging.getLogger(__name__))

    assert "skipped without a running loop" in caplog.text
