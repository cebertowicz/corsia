"""One runnable predicate decides hold and release of an unavailable model (#2525)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from loguru import logger

from tests.unavailable_model_support import (
    HELD,
    admit_held_candidate,
    hold_head,
    message_row,
    seed_model,
    set_model_available,
)


@pytest.fixture
def driven(monkeypatch):
    """Record the batches the idle driver would run, without dispatching them."""
    import infra.thread_execution as execution

    drive = AsyncMock()
    monkeypatch.setattr(execution, "drive_claimed_batch", drive)
    return drive


@pytest.fixture
def records():
    seen: list = []
    sink = logger.add(seen.append, level="WARNING")
    yield seen
    logger.remove(sink)


def _active_cloud(monkeypatch):
    monkeypatch.setattr("llm.model_status._cloud_providers_active", lambda: {"anthropic"})


@pytest.mark.asyncio
async def test_available_but_unloaded_network_model_releases_its_held_head(
    isolated_db, monkeypatch, driven,
) -> None:
    """Resolution runs an available oMLX model (it loads on demand); the catalog
    status only says 'available'. Release must follow resolution."""
    import infra.unavailable_model_recovery as recovery
    from llm.model_status import get_model_status_rows
    from llm.resolution import resolve_execution_target

    await hold_head(monkeypatch, "t", "omlx@srv/m", "m1")
    set_model_available("omlx@srv/m", 1)
    monkeypatch.setattr("llm.model_status._network_server_status", lambda *_a: {"srv": "online"})
    monkeypatch.setattr("llm.model_status._loaded_model_ids_fn", lambda: lambda _sid: set())
    assert resolve_execution_target("omlx@srv/m").available is True
    statuses = {row["id"]: row["status"] for row in get_model_status_rows()}
    assert statuses["omlx@srv/m"] == "available"

    await recovery._resume_held_heads()

    driven.assert_awaited_once()


@pytest.mark.asyncio
async def test_cloud_row_runnable_by_resolution_releases_though_status_omits_it(
    isolated_db, monkeypatch, driven,
) -> None:
    """A cloud row whose provider is marked inactive is left out of the status
    list, yet resolution dispatches it (the provider then answers). Release
    follows resolution, so the head fails or runs visibly instead of waiting."""
    import infra.unavailable_model_recovery as recovery
    from llm.model_status import get_model_status_rows
    from llm.resolution import resolve_execution_target

    await hold_head(monkeypatch, "t", "anthropic/x", "m1", backend="api", provider="anthropic")
    set_model_available("anthropic/x", 1)
    monkeypatch.setattr("llm.model_status._cloud_providers_active", lambda: set())
    assert resolve_execution_target("anthropic/x").available is True
    assert "anthropic/x" not in {row["id"] for row in get_model_status_rows()}

    await recovery._resume_held_heads()

    driven.assert_awaited_once()


@pytest.mark.asyncio
async def test_unavailable_cloud_row_stays_held(isolated_db, monkeypatch, driven) -> None:
    import infra.unavailable_model_recovery as recovery
    from llm.resolution import resolve_execution_target

    _active_cloud(monkeypatch)
    await hold_head(monkeypatch, "t", "anthropic/x", "m1", backend="api", provider="anthropic")
    assert resolve_execution_target("anthropic/x").available is False

    await recovery._resume_held_heads()

    driven.assert_not_awaited()
    assert message_row("m1") == HELD


@pytest.mark.asyncio
async def test_offline_network_server_stays_held(isolated_db, monkeypatch, driven) -> None:
    import infra.unavailable_model_recovery as recovery

    monkeypatch.setattr("llm.model_status._network_server_status", lambda *_a: {"srv": "offline"})
    await hold_head(monkeypatch, "t", "omlx@srv/m", "m1")

    await recovery._resume_held_heads()

    driven.assert_not_awaited()
    assert message_row("m1") == HELD


@pytest.mark.asyncio
async def test_head_whose_model_left_the_catalog_is_released_to_fail_visibly(
    isolated_db, monkeypatch, driven,
) -> None:
    """A permanent resolution failure is not a wait; the driver fails it visibly."""
    from infra.database import get_db
    import infra.unavailable_model_recovery as recovery

    await hold_head(monkeypatch, "t", "omlx@srv/m", "m1")
    db = get_db()
    db.execute("DELETE FROM models WHERE id='omlx@srv/m'")
    db.commit()
    db.close()

    await recovery._resume_held_heads()

    driven.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_catalog_model_fails_the_turn_and_is_not_held(
    isolated_db, monkeypatch,
) -> None:
    import infra.thread_execution as execution
    from infra.send_target import TargetUnavailable
    from infra.send_types import DeliveryFailureSide

    batch = admit_held_candidate("t", "omlx@srv/gone", "m1")

    with pytest.raises(TargetUnavailable) as failed:
        await execution.drive_claimed_batch(batch)

    assert failed.value.side is DeliveryFailureSide.OURS

    assert message_row("m1")[2] != "selected_model_unavailable"
    assert message_row("m1")[0] == "failed"


@pytest.mark.asyncio
async def test_connection_unavailable_fails_visibly_and_is_not_held(
    isolated_db, monkeypatch,
) -> None:
    """A harness connection state has no model-change wake, so it cannot enter
    the model-recovery hold even though the catalog row itself is available."""
    from infra.database import get_db
    import infra.thread_execution as execution
    from infra.send_target import TargetUnavailable
    from infra.send_types import DeliveryFailureSide

    db = get_db()
    try:
        db.execute(
            "INSERT INTO models (id,harness,provider,provider_model_id,"
            "litellm_model,display_name,backend,enabled,available,"
            "context_window,created_at,updated_at) "
            "VALUES ('claude-code/test','claude-code','claude-code','test',"
            "'claude-code/test','Test','cc',1,1,32768,'t0','t0')"
        )
        db.execute(
            "UPDATE connections SET status='needs_auth' WHERE provider='claude-code'"
        )
        db.commit()
    finally:
        db.close()
    batch = admit_held_candidate("t", "claude-code/test", "m1")

    with pytest.raises(TargetUnavailable) as failed:
        await execution.drive_claimed_batch(batch)

    assert failed.value.side is DeliveryFailureSide.PROVIDER
    assert message_row("m1")[0] == "failed"
    assert message_row("m1")[2] != "selected_model_unavailable"


@pytest.mark.asyncio
async def test_unavailable_catalog_row_is_still_held(isolated_db, monkeypatch) -> None:
    await hold_head(monkeypatch, "t", "omlx@srv/m", "m1")

    assert message_row("m1") == HELD


@pytest.mark.asyncio
async def test_send_path_fails_a_claim_whose_model_is_not_configured(
    isolated_db, monkeypatch,
) -> None:
    """Our own catalog defect is a visible SendRejected for the sender, not a
    queued receipt for a message that can never be delivered."""
    from infra.database import get_db
    import infra.send_service as service
    from infra.send_target import TargetUnavailable
    from infra.send_types import DeliveryFailureSide

    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,model,created_at,updated_at) "
        "VALUES ('t','user-1','omlx@srv/gone','t0','t0')"
    )
    db.commit()
    thread_row = dict(db.execute("SELECT * FROM threads WHERE id='t'").fetchone())
    db.close()

    async def not_configured(_model):
        raise TargetUnavailable("No model configured", side=DeliveryFailureSide.OURS)

    monkeypatch.setattr("infra.send_target.resolve_target", not_configured)
    monkeypatch.setattr(service, "broadcast_accepted_input", lambda *_a, **_k: None)

    with pytest.raises(service.SendRejected) as rejected:
        await service.send_thread_input(
            "t", thread_row, "hello", user={"id": "user-1"},
            input_id="m1", native_mode="ws", ws=object(),
        )

    assert rejected.value.input_undeliverable is True
    assert message_row("m1")[0] == "failed"


@pytest.mark.asyncio
async def test_model_ready_between_resolution_and_hold_still_drains(
    isolated_db, monkeypatch,
) -> None:
    """The catalog notification fires while the input is still claimed and is
    skipped; the hold's own settlement claim must read the catalog again."""
    import infra.thread_execution as execution
    from llm import resolution

    seed_model("omlx@srv/m", available=0)
    batch = admit_held_candidate("t", "omlx@srv/m", "m1")
    run = AsyncMock()
    monkeypatch.setattr(execution, "execute_claimed_batch", run)
    real = resolution.resolve_execution_target
    calls: list[int] = []

    def resolve_then_recover(model, **kwargs):
        target = real(model, **kwargs)
        if not calls:
            calls.append(1)
            set_model_available("omlx@srv/m", 1)
        return target

    monkeypatch.setattr(resolution, "resolve_execution_target", resolve_then_recover)

    await execution.drive_claimed_batch(batch)

    run.assert_awaited_once()
    assert message_row("m1")[:3] == ("queued", None, None)


@pytest.mark.asyncio
async def test_one_threads_recovery_failure_does_not_stop_the_others(
    isolated_db, monkeypatch, records,
) -> None:
    import infra.thread_execution as execution
    import infra.unavailable_model_recovery as recovery

    monkeypatch.setattr(recovery, "_held_thread_ids", lambda: ["a", "b"])
    drive = AsyncMock(side_effect=[RuntimeError("drive boom"), None])
    monkeypatch.setattr(execution, "drive_idle_thread", drive)

    await recovery._resume_held_heads()

    assert [call.args for call in drive.await_args_list] == [("a",), ("b",)]
    failures = [r for r in records if r.record["exception"] is not None]
    assert len(failures) == 1
    assert "a" in failures[0].record["message"]


@pytest.mark.asyncio
async def test_recovery_task_failure_is_observed(isolated_db, monkeypatch, records) -> None:
    import infra.unavailable_model_recovery as recovery

    def broken():
        raise RuntimeError("scan boom")

    monkeypatch.setattr(recovery, "_TASK", None)
    monkeypatch.setattr(recovery, "_held_thread_ids", broken)

    recovery._schedule_on_loop()
    await asyncio.wait([recovery._TASK])
    await asyncio.sleep(0)

    assert any(
        r.record["exception"] is not None
        and "scan boom" in str(r.record["exception"].value)
        for r in records
    )


def test_scheduling_without_a_known_loop_is_not_silent(monkeypatch, records) -> None:
    import infra.unavailable_model_recovery as recovery
    import infra.app_loop as app_loop

    monkeypatch.setattr(app_loop, "_LOOP", None)

    recovery.schedule_unavailable_model_recovery()

    assert records


@pytest.mark.asyncio
async def test_discovery_owner_uses_the_public_models_changed_notifier(monkeypatch) -> None:
    from connections import harness_models
    import llm.context_window_discovery as context_window
    import llm.network_discovery as network_discovery

    public = Mock()
    monkeypatch.setattr("infra.state.ws.notify_models_changed", public)
    monkeypatch.setattr(
        network_discovery, "_notify_models_changed_safe",
        Mock(side_effect=AssertionError("private helper used")),
    )
    monkeypatch.setattr(harness_models, "refresh_provider", AsyncMock())

    await context_window.run_discovery_owner(
        "claude-code/sonnet", {"harness": "claude-code", "enabled": 1, "available": 1},
    )

    public.assert_called_once_with()
