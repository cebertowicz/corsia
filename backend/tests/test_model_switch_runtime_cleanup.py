"""Runtime-cleanup contracts for already-committed model switches."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.asyncio
async def test_codex_captured_owner_already_gone_is_a_success(monkeypatch, caplog) -> None:
    """The captured source cannot run again when no Codex handle remains."""
    from subagents import codex_retirement, codex_runtime

    monkeypatch.setattr(codex_runtime, "current_handle_fingerprint", lambda _thread_id: None)
    caplog.set_level(logging.INFO, logger=codex_retirement.__name__)

    assert await codex_runtime.retire_if_current(
        "thread-1", expected_owner=object(), operation_id="switch-1",
    ) is True
    assert "branch=owner_gone thread_id=thread-1 operation_id=switch-1" in caplog.text


@pytest.mark.asyncio
async def test_claude_captured_owner_already_gone_retires_durable_session(monkeypatch) -> None:
    """An exited Claude process still needs its durable provider session retired."""
    from subagents import cc_handle

    retired = AsyncMock(return_value=True)
    monkeypatch.setattr(cc_handle, "run_in_db", retired)

    assert await cc_handle.retire_if_current(
        "thread-1", expected_owner=object(),
    ) is True
    retired.assert_awaited_once()


@pytest.mark.asyncio
async def test_claude_different_owner_remains_a_failure_with_diagnostics(monkeypatch, caplog) -> None:
    """A new live Claude owner is never retired by an older switch."""
    from subagents import cc_handle
    from subagents.cc_runtime_state import _Live

    live = _Live(session=None, parent_thread_id=None, name="test")
    cc_handle._LIVE.put("thread-1", live)
    monkeypatch.setattr(cc_handle, "live_handle_fingerprint", lambda *_args: "new-owner")
    caplog.set_level(logging.WARNING, logger=cc_handle.__name__)
    try:
        assert await cc_handle.retire_if_current(
            "thread-1", expected_owner="old-owner", operation_id="switch-1",
        ) is False
    finally:
        cc_handle._LIVE.pop("thread-1", None)
    assert "branch=owner_changed thread_id=thread-1 operation_id=switch-1" in caplog.text


@pytest.mark.asyncio
async def test_claude_retirement_failure_remains_a_failure_with_diagnostics(monkeypatch, caplog) -> None:
    """A matching Claude owner is not treated as retired until cleanup succeeds."""
    from subagents import cc_handle, cc_shutdown
    from subagents.cc_runtime_state import _Live

    live = _Live(session=None, parent_thread_id=None, name="test")
    cc_handle._LIVE.put("thread-1", live)
    monkeypatch.setattr(cc_handle, "live_handle_fingerprint", lambda *_args: "owner")
    monkeypatch.setattr(cc_shutdown, "retire_live", AsyncMock(return_value=False))
    caplog.set_level(logging.WARNING, logger=cc_handle.__name__)
    try:
        assert await cc_handle.retire_if_current(
            "thread-1", expected_owner="owner", operation_id="switch-1",
        ) is False
    finally:
        cc_handle._LIVE.pop("thread-1", None)
    assert "branch=retire_failed thread_id=thread-1 operation_id=switch-1" in caplog.text


@pytest.mark.asyncio
async def test_codex_different_owner_remains_a_failure_with_diagnostics(monkeypatch, caplog) -> None:
    """A new live owner is never retired by an older switch."""
    from subagents import codex_retirement, codex_runtime

    monkeypatch.setattr(codex_runtime, "current_handle_fingerprint", lambda _thread_id: "new-owner")
    caplog.set_level(logging.WARNING, logger=codex_retirement.__name__)

    assert await codex_runtime.retire_if_current(
        "thread-1", expected_owner="old-owner", operation_id="switch-1",
    ) is False
    assert "branch=owner_changed thread_id=thread-1 operation_id=switch-1" in caplog.text


@pytest.mark.asyncio
async def test_codex_retirement_failure_remains_a_failure_with_diagnostics(monkeypatch, caplog) -> None:
    """A matching owner is not treated as retired until its cleanup succeeds."""
    from subagents import codex_retirement, codex_runtime

    monkeypatch.setattr(codex_runtime, "current_handle_fingerprint", lambda _thread_id: "owner")
    monkeypatch.setattr(codex_runtime, "_retire_locked", AsyncMock(return_value=False))
    caplog.set_level(logging.WARNING, logger=codex_retirement.__name__)

    assert await codex_runtime.retire_if_current(
        "thread-1", expected_owner="owner", operation_id="switch-1",
    ) is False
    assert "branch=retire_failed thread_id=thread-1 operation_id=switch-1" in caplog.text


@pytest.mark.asyncio
async def test_committed_switch_publishes_selection_before_surface_cleanup_warning(monkeypatch) -> None:
    """The UI must learn durable model truth even when source cleanup refuses."""
    from thread import model_runtime

    transition = AsyncMock(return_value=False)
    selection = Mock()
    product_event = Mock()
    breadcrumb = AsyncMock()
    monkeypatch.setattr(model_runtime, "_transition_source_runtime", transition)
    monkeypatch.setattr(model_runtime, "_broadcast_model_selection", selection)
    monkeypatch.setattr(model_runtime, "emit_product_event", product_event)
    monkeypatch.setattr(model_runtime, "_broadcast_model_switch_breadcrumb", breadcrumb)

    with pytest.raises(RuntimeError, match="source runtime owner changed"):
        await model_runtime.post_model_switch_effects(
            "thread-1",
            new_model="claude-code/opus",
            user_id="user-1",
            source_harness="codex",
            source_runtime_owner="owner",
            operation_id="switch-1",
            breadcrumb={"id": "receipt-1"},
            committed_at="2026-10-13T18:14:17+00:00",
        )

    transition.assert_awaited_once()
    selection.assert_called_once_with(
        "thread-1",
        new_model="claude-code/opus",
        user_id="user-1",
        committed_at="2026-10-13T18:14:17+00:00",
    )
    product_event.assert_called_once()
    breadcrumb.assert_awaited_once_with("thread-1", {"id": "receipt-1"})
