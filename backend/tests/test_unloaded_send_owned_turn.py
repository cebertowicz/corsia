"""Failed or stopped loads settle the claimed input through the native Turn owner."""

import asyncio
from unittest.mock import AsyncMock

import pytest


def _claimed(monkeypatch, *, metadata=None, native_mode="event"):
    from infra.database import get_db
    from dataclasses import replace
    from infra.input_render import PreparedBatch
    from infra.thread_driver import admit_input
    from llm.local_model_load import Readiness
    from llm.resolution import resolve_execution_target
    from tests.unavailable_model_support import seed_model

    seed_model("omlx@srv/m")
    db = get_db()
    db.execute("INSERT INTO threads (id,user_id,model,created_at,updated_at) VALUES ('t','u','omlx@srv/m','t0','t0')")
    db.commit()
    target = resolve_execution_target("omlx@srv/m", db=db)
    monkeypatch.setattr("llm.local_model_load.readiness", lambda *_a, **_k: Readiness("fits_now"))
    batch = admit_input("t", role="user", content="hi", intent="apply_now", message_id="m", turn_id="turn", boot_epoch="boot", admitted_at="t1", metadata=metadata, delivery_options={"native_mode": native_mode}, db=db).batch
    thread_row = dict(db.execute("SELECT * FROM threads WHERE id='t'").fetchone())
    db.close()
    target = replace(target, available=True, availability_reason="load_on_send")
    async def prepare(batch, *, thread_row, target):
        return PreparedBatch(thread_row, target, "native", False, "hi", None, [], None, None, None, {}, batch)
    monkeypatch.setattr("infra.thread_execution._prepare_batch", prepare)
    monkeypatch.setattr("infra.thread_execution.persist_prepared_installed_context", lambda *_a: None)
    monkeypatch.setattr("infra.thread_execution.record_dispatch_manifest", lambda *_a: None)
    monkeypatch.setattr("core.agent.get_agent", AsyncMock(return_value=object()))
    monkeypatch.setattr("ws.turn.make_provider_params", lambda _model: {})
    monkeypatch.setattr("ws.turn.native_provider_params", lambda *_a: {})
    provider = AsyncMock()
    monkeypatch.setattr("ws.turn._run_prepared_turn", provider)
    return batch, thread_row, target, provider


@pytest.mark.asyncio
async def test_person_send_to_a_cloud_model_checks_before_binding(monkeypatch):
    from types import SimpleNamespace

    import ws.turn as turn

    target = SimpleNamespace(harness="native", provider="anthropic", backend="api", transport_model="claude-x", availability_reason=None)
    request = SimpleNamespace(target=target, ingress="ws")
    bind = AsyncMock()
    load = AsyncMock()
    monkeypatch.setattr("core.agent.get_agent", AsyncMock(return_value=object()))
    monkeypatch.setattr(turn, "validate_native_target", AsyncMock(side_effect=RuntimeError("unhealthy")))
    monkeypatch.setattr(turn, "persist_turn_start_bound", bind)
    monkeypatch.setattr(turn, "_load_for_person", load)
    with pytest.raises(RuntimeError, match="unhealthy"):
        await turn._setup_turn("t", "hi", [], target=target, turn_id="turn", ingress="ws", request=request)
    bind.assert_not_awaited()
    load.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cause, expected", [("load_failed", "model_load_failed"), ("unreachable", "model_load_unreachable")])
@pytest.mark.parametrize("native_mode, metadata", [("event", {"retried_from_message_id": "old"}), ("ws", None)])
async def test_failed_load_settles_input(isolated_db, monkeypatch, cause, expected, native_mode, metadata):
    from infra.thread_execution import execute_claimed_batch
    from llm.local_model_load import ModelLoadFailed
    from tests.unavailable_model_support import message_row

    batch, thread, target, provider = _claimed(monkeypatch, metadata=metadata, native_mode=native_mode)
    load = AsyncMock(side_effect=ModelLoadFailed(cause))
    monkeypatch.setattr("llm.local_model_load.ensure_loaded", load)
    with pytest.raises(Exception):
        await execute_claimed_batch(batch, thread_row=thread, target=target, user={"id": "u"})
    assert message_row("m")[:3] == ("failed", "proved_no_write", expected)
    load.assert_awaited_once_with(target)
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_route_reloads_failed_person_input(isolated_db, monkeypatch):
    from api.thread_inbox_actions import retry_thread_delivery_now
    from infra.input_render import PreparedBatch
    from infra.thread_execution import execute_claimed_batch
    from llm.local_model_load import ModelLoadFailed
    from tests.unavailable_model_support import message_row

    batch, thread, target, _provider = _claimed(monkeypatch)
    load = AsyncMock(side_effect=[ModelLoadFailed("load_failed"), None])
    monkeypatch.setattr("llm.local_model_load.ensure_loaded", load)
    with pytest.raises(Exception):
        await execute_claimed_batch(batch, thread_row=thread, target=target, user={"id": "u"})
    assert message_row("m")[:3] == ("failed", "proved_no_write", "model_load_failed")

    async def prepare(next_batch, *, thread_row, target):
        return PreparedBatch(thread_row, target, "native", False, "hi", None, [], None, None, None, {}, next_batch)

    accepted = asyncio.Event()

    async def run(request, _prepared):
        await request.settle_delivery()
        accepted.set()
        return False

    monkeypatch.setattr("infra.thread_execution._prepare_batch", prepare)
    monkeypatch.setattr("ws.turn.validate_native_target", AsyncMock())
    monkeypatch.setattr("ws.turn._run_prepared_turn", run)
    monkeypatch.setattr("ws.turn.run_post_turn_tasks", AsyncMock())
    result = await retry_thread_delivery_now("t", "m", "u")
    await asyncio.wait_for(accepted.wait(), 5)
    assert load.await_count == 2
    assert message_row(result["revived_message_id"])[0] == "delivered"


@pytest.mark.asyncio
async def test_queued_input_that_no_longer_fits_fails_at_claim(isolated_db, monkeypatch):
    from infra.thread_execution import drive_claimed_batch
    from llm.local_model_load import Readiness
    from tests.unavailable_model_support import message_row

    batch, _thread, _target, provider = _claimed(monkeypatch)
    monkeypatch.setattr("llm.local_model_load.readiness", lambda *_a, **_k: Readiness("no_room", 1))
    with pytest.raises(Exception):
        await drive_claimed_batch(batch)
    assert message_row("m")[:3] == ("failed", "proved_no_write", "model_load_failed")
    from infra.database import get_db
    db = get_db()
    try:
        assert db.execute("SELECT json_extract(metadata, '$.model_load_cause') FROM messages WHERE id='m'").fetchone()[0] == "no_room"
    finally:
        db.close()
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_person_send_loads_before_provider_acceptance(isolated_db, monkeypatch):
    from infra.thread_execution import execute_claimed_batch
    from tests.unavailable_model_support import message_row

    batch, thread, target, _provider = _claimed(monkeypatch)
    steps = []

    async def load(_target):
        steps.append("load")

    async def run(request, _prepared):
        steps.append("provider")
        await request.settle_delivery()
        return False

    monkeypatch.setattr("llm.local_model_load.ensure_loaded", load)
    monkeypatch.setattr("ws.turn.validate_native_target", AsyncMock())
    monkeypatch.setattr("ws.turn._run_prepared_turn", run)
    monkeypatch.setattr("ws.turn.run_post_turn_tasks", AsyncMock())
    result = await execute_claimed_batch(batch, thread_row=thread, target=target, user={"id": "u"})
    await result.turn_task
    assert steps == ["load", "provider"]
    assert message_row("m")[0] == "delivered"


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [{"sender_kind": "eval"}, {"sender_kind": "agent", "from_thread": "parent"}])
async def test_nonhuman_input_does_not_load(isolated_db, monkeypatch, metadata):
    from infra.thread_execution import drive_claimed_batch
    from infra import unavailable_model_recovery as recovery
    from tests.unavailable_model_support import mark_model_loaded, message_row

    batch, _thread, _target, provider = _claimed(monkeypatch, metadata=metadata)
    mark_model_loaded(monkeypatch, "omlx@srv/m", loaded=False)
    load = AsyncMock()
    monkeypatch.setattr("llm.local_model_load.ensure_loaded", load)
    monkeypatch.setattr("infra.unavailable_model_recovery.schedule_unavailable_model_recovery", lambda: None)
    held = AsyncMock(wraps=recovery.hold_unavailable_model_input)
    monkeypatch.setattr(recovery, "hold_unavailable_model_input", held)
    await drive_claimed_batch(batch)
    assert held.await_args.args[1].availability_reason == "fits_now"
    load.assert_not_awaited()
    provider.assert_not_awaited()
    assert message_row("m") == ("queued", "proved_no_write", "selected_model_unavailable", None)


@pytest.mark.asyncio
async def test_validation_failure_after_bind_settles_input(isolated_db, monkeypatch):
    from infra.thread_execution import execute_claimed_batch
    from llm.local_model_load import Readiness
    from tests.unavailable_model_support import message_row

    batch, thread, target, provider = _claimed(monkeypatch)
    monkeypatch.setattr("llm.local_model_load.readiness", lambda *_a, **_k: Readiness("loaded"))
    monkeypatch.setattr("llm.local_model_load.ensure_loaded", AsyncMock())
    monkeypatch.setattr("ws.turn.validate_native_target", AsyncMock(side_effect=RuntimeError("unhealthy")))
    with pytest.raises(Exception, match="unhealthy"):
        await execute_claimed_batch(batch, thread_row=thread, target=target, user={"id": "u"})
    assert message_row("m")[:3] == ("failed", "proved_no_write", "unhealthy")
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_unavailable_after_load_uses_standard_presentation(isolated_db, monkeypatch):
    from infra.thread_execution import execute_claimed_batch
    from llm.resolution import ModelUnavailableError
    from tests.unavailable_model_support import message_row
    from ws import turn

    batch, thread, target, provider = _claimed(monkeypatch)
    error = ModelUnavailableError(target.catalog_id, "offline")
    monkeypatch.setattr("llm.local_model_load.ensure_loaded", AsyncMock())
    monkeypatch.setattr(turn, "validate_native_target", AsyncMock(side_effect=error))
    report = AsyncMock(wraps=turn.report_model_unavailable)
    monkeypatch.setattr(turn, "report_model_unavailable", report)
    with pytest.raises(ModelUnavailableError):
        await execute_claimed_batch(batch, thread_row=thread, target=target, user={"id": "u"})
    report.assert_awaited_once()
    assert report.await_args.args[1] is error
    assert message_row("m")[:2] == ("failed", "proved_no_write")
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_during_load_settles_cancelled_input(isolated_db, monkeypatch):
    from core.native_turn_control import cancel_and_join_native_turn
    from infra.thread_execution import execute_claimed_batch
    from tests.unavailable_model_support import message_row

    batch, thread, target, provider = _claimed(monkeypatch)
    started = asyncio.Event()
    done = asyncio.get_running_loop().create_future()

    async def load(_target):
        started.set()
        await asyncio.shield(done)

    monkeypatch.setattr("llm.local_model_load.ensure_loaded", load)
    task = asyncio.create_task(execute_claimed_batch(batch, thread_row=thread, target=target, user={"id": "u"}))
    await asyncio.wait_for(started.wait(), 5)
    assert await cancel_and_join_native_turn("t", replacement_requested=False, timeout_s=5)
    await asyncio.gather(task, return_exceptions=True)
    assert message_row("m")[:2] == ("cancelled", "proved_no_write")
    assert not done.done()
    provider.assert_not_awaited()
    done.set_result(None)
    await asyncio.sleep(0)
