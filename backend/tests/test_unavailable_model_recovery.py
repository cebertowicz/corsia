"""Regression coverage for unavailable selected-model FIFO recovery (#2525)."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from tests.unavailable_model_support import (
    HELD,
    hold_head,
    message_row,
    mark_model_loaded,
    set_model_available,
)


@pytest.fixture
def driven(monkeypatch):
    """Record the batches the idle driver would run, without dispatching them."""
    import infra.thread_execution as execution

    drive = AsyncMock()
    monkeypatch.setattr(execution, "drive_claimed_batch", drive)
    return drive


@pytest.mark.asyncio
async def test_ready_notification_releases_and_drives_the_held_head_once(
    isolated_db, monkeypatch, driven,
) -> None:
    import infra.unavailable_model_recovery as recovery

    await hold_head(monkeypatch, "t", "omlx@srv/m", "m1")
    set_model_available("omlx@srv/m", 1)
    mark_model_loaded(monkeypatch, "omlx@srv/m")

    await recovery._resume_held_heads()
    await recovery._resume_held_heads()

    driven.assert_awaited_once()
    assert "m1" in [item.message_id for item in driven.await_args.args[0].items]
    assert message_row("m1")[:3] == ("queued", None, None)


@pytest.mark.asyncio
async def test_notification_with_no_held_head_does_not_drive(
    isolated_db, monkeypatch, driven,
) -> None:
    import infra.unavailable_model_recovery as recovery

    await recovery._resume_held_heads()

    driven.assert_not_awaited()


@pytest.mark.asyncio
async def test_flap_keeps_fifo_held_until_ready_then_drives_once(
    isolated_db, monkeypatch, driven,
) -> None:
    import infra.unavailable_model_recovery as recovery

    await hold_head(monkeypatch, "t", "omlx@srv/m", "m1")

    await recovery._resume_held_heads()
    driven.assert_not_awaited()
    assert message_row("m1") == HELD

    set_model_available("omlx@srv/m", 1)
    mark_model_loaded(monkeypatch, "omlx@srv/m")
    await recovery._resume_held_heads()

    driven.assert_awaited_once()


@pytest.mark.asyncio
async def test_discovery_owner_notifies_for_harness_and_network_rows(monkeypatch):
    """Both discovery branches notify so a recovered model can wake held heads."""
    from connections import harness_models
    from llm import catalog_network_models
    import llm.context_window_discovery as context_window

    notify = Mock()
    monkeypatch.setattr("infra.state.ws.notify_models_changed", notify)
    monkeypatch.setattr(harness_models, "refresh_provider", AsyncMock())
    monkeypatch.setattr(catalog_network_models, "discover_network_models", AsyncMock())

    await context_window.run_discovery_owner(
        "claude-code/sonnet",
        {"harness": "claude-code", "enabled": 1, "available": 1},
    )
    await context_window.run_discovery_owner(
        "omlx@server/model", {"litellm_model": "omlx@server/model"},
    )

    assert notify.call_count == 2


@pytest.mark.asyncio
async def test_discover_cloud_models_notifies_when_catalog_changes(monkeypatch):
    """A cloud catalog refresh wakes any thread held on a now-listed model."""
    import llm.catalog_cloud as catalog_cloud
    from infra.state import ws

    monkeypatch.setattr(catalog_cloud, "_anthropic_discovered", lambda: [])
    monkeypatch.setattr(catalog_cloud, "_persist_cloud_models", lambda *_a, **_k: [])
    notify = Mock()
    monkeypatch.setattr(ws, "notify_models_changed", notify)

    result = await catalog_cloud.discover_cloud_models("anthropic", "test-key")

    assert result["ok"] is True
    notify.assert_called_once_with()


@pytest.mark.asyncio
async def test_boot_recovery_claims_a_previously_held_input(
    isolated_db, monkeypatch,
) -> None:
    """An input held before a restart is never lost; the boot pass claims it
    with the same predicate once the model is ready."""
    from infra.thread_recovery import recover_threads

    await hold_head(monkeypatch, "boot-thread", "omlx@srv/m", "boot-input")

    still_down = recover_threads(
        boot_epoch="boot-2", recovered_at="t9", successor_id=lambda _t: "boot-turn-0",
    )
    assert not still_down.batches
    assert message_row("boot-input") == HELD

    set_model_available("omlx@srv/m", 1)
    mark_model_loaded(monkeypatch, "omlx@srv/m")
    recovered = recover_threads(
        boot_epoch="boot-2", recovered_at="t9", successor_id=lambda _t: "boot-turn-1",
    )

    assert "boot-input" in [item.message_id for item in recovered.batches[0].items]
    assert message_row("boot-input") == ("queued", None, None, "boot-turn-1")
