"""The owned native turn waits for the local model without writing to the provider."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.asyncio
async def test_load_reports_event_and_waits(monkeypatch):
    from ws.local_send_load import load_for_person as _load_for_person

    target = SimpleNamespace(catalog_id="omlx@srv/m")
    request = SimpleNamespace(thread_id="t", target=target, input_ids=("a", "b"))
    prepared = SimpleNamespace(identity=SimpleNamespace(turn_id="turn"), cancel_event=asyncio.Event())
    monkeypatch.setattr("llm.local_model_load.omlx_seat_and_model", lambda _target: ("srv", "m"))
    monkeypatch.setattr("llm.network_discovery.get_loaded_model_ids", lambda _server: set())
    load = AsyncMock()
    broadcast = Mock()
    monkeypatch.setattr("llm.local_model_load.ensure_loaded", load)
    monkeypatch.setattr("infra.state.broadcast_to_thread", broadcast)
    await _load_for_person(request, prepared)
    load.assert_awaited_once_with(target)
    broadcast.assert_called_once_with("t", {
        "type": "model_loading", "thread_id": "t", "turn_id": "turn",
        "model": "omlx@srv/m", "input_ids": ["a", "b"],
    })


@pytest.mark.asyncio
async def test_cached_loaded_still_confirms_live_without_loading_event(monkeypatch):
    from ws.local_send_load import load_for_person as _load_for_person

    target = SimpleNamespace(catalog_id="omlx@srv/m")
    request = SimpleNamespace(thread_id="t", target=target, input_ids=("a",))
    prepared = SimpleNamespace(identity=SimpleNamespace(turn_id="turn"), cancel_event=asyncio.Event())
    monkeypatch.setattr("llm.local_model_load.omlx_seat_and_model", lambda _target: ("srv", "m"))
    monkeypatch.setattr("llm.network_discovery.get_loaded_model_ids", lambda _server: {"m"})
    load = AsyncMock()
    broadcast = Mock()
    monkeypatch.setattr("llm.local_model_load.ensure_loaded", load)
    monkeypatch.setattr("infra.state.broadcast_to_thread", broadcast)
    await _load_for_person(request, prepared)
    load.assert_awaited_once_with(target)
    broadcast.assert_not_called()


@pytest.mark.asyncio
async def test_out_of_scope_target_is_passed_to_loader(monkeypatch):
    from ws.local_send_load import load_for_person as _load_for_person

    request = SimpleNamespace(thread_id="t", target=SimpleNamespace(catalog_id="omlx@srv/m"), input_ids=("a",))
    prepared = SimpleNamespace(identity=SimpleNamespace(turn_id="turn"), cancel_event=asyncio.Event())
    monkeypatch.setattr("llm.local_model_load.omlx_seat_and_model", lambda _target: None)
    load = AsyncMock()
    monkeypatch.setattr("llm.local_model_load.ensure_loaded", load)
    await _load_for_person(request, prepared)
    load.assert_awaited_once_with(request.target)


@pytest.mark.asyncio
@pytest.mark.parametrize("cause, expected", [("load_failed", "model_load_failed"), ("unreachable", "model_load_unreachable")])
async def test_load_failure_is_mapped(monkeypatch, cause, expected):
    from llm.local_model_load import ModelLoadFailed
    from ws.local_send_load import load_for_person as _load_for_person

    target = SimpleNamespace(catalog_id="omlx@srv/m")
    request = SimpleNamespace(thread_id="t", target=target, input_ids=("a",))
    prepared = SimpleNamespace(identity=SimpleNamespace(turn_id="turn"), cancel_event=asyncio.Event())
    monkeypatch.setattr("llm.local_model_load.omlx_seat_and_model", lambda _target: ("srv", "m"))
    monkeypatch.setattr("llm.network_discovery.get_loaded_model_ids", lambda _server: set())
    monkeypatch.setattr("llm.local_model_load.ensure_loaded", AsyncMock(side_effect=ModelLoadFailed(cause)))
    monkeypatch.setattr("infra.state.broadcast_to_thread", Mock())
    from infra.send_target import model_load_error
    with pytest.raises(ModelLoadFailed) as raised:
        await _load_for_person(request, prepared)
    assert model_load_error(raised.value.cause) == expected


@pytest.mark.asyncio
async def test_stop_abandons_wait_without_cancelling_load(monkeypatch):
    from ws.local_send_load import load_for_person as _load_for_person

    target = SimpleNamespace(catalog_id="omlx@srv/m")
    request = SimpleNamespace(thread_id="t", target=target, input_ids=("a",))
    prepared = SimpleNamespace(identity=SimpleNamespace(turn_id="turn"), cancel_event=asyncio.Event())
    pending = asyncio.get_running_loop().create_future()
    started = asyncio.Event()

    async def load(_target):
        started.set()
        await asyncio.shield(pending)

    monkeypatch.setattr("llm.local_model_load.omlx_seat_and_model", lambda _target: ("srv", "m"))
    monkeypatch.setattr("llm.network_discovery.get_loaded_model_ids", lambda _server: set())
    monkeypatch.setattr("llm.local_model_load.ensure_loaded", load)
    monkeypatch.setattr("infra.state.broadcast_to_thread", Mock())
    turn = asyncio.create_task(_load_for_person(request, prepared))
    await started.wait()
    prepared.cancel_event.set()
    with pytest.raises(asyncio.CancelledError):
        await turn
    assert not pending.done()
    pending.set_result(None)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_detached_load_failure_after_stop_is_logged(monkeypatch, caplog):
    from ws.local_send_load import load_for_person

    request = SimpleNamespace(thread_id="t", target=SimpleNamespace(catalog_id="omlx@srv/m"), input_ids=("a",))
    prepared = SimpleNamespace(identity=SimpleNamespace(turn_id="turn"), cancel_event=asyncio.Event())
    pending = asyncio.get_running_loop().create_future()
    started = asyncio.Event()

    async def preliminary(*_args):
        started.set()
        return await pending

    errors = []
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "call_exception_handler", errors.append)
    monkeypatch.setattr("llm.local_model_load.omlx_seat_and_model", lambda _target: ("srv", "m"))
    monkeypatch.setattr("llm.network_discovery.get_loaded_model_ids", lambda _server: set())
    monkeypatch.setattr("infra.db_async.run_in_db", preliminary)
    monkeypatch.setattr("infra.state.broadcast_to_thread", Mock())
    turn = asyncio.create_task(load_for_person(request, prepared))
    await started.wait()
    prepared.cancel_event.set()
    with pytest.raises(asyncio.CancelledError):
        await turn
    pending.set_exception(ValueError("detached failure"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert caplog.text.count("Loading m on srv failed unexpectedly") == 1
    assert "detached failure" in caplog.text
    assert not errors
