"""The owned native turn waits for the local model without writing to the provider."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.asyncio
async def test_load_reports_event_and_waits(monkeypatch):
    from llm.local_model_load import Readiness
    from ws.local_send_load import load_for_person as _load_for_person

    target = SimpleNamespace(catalog_id="omlx@srv/m")
    request = SimpleNamespace(thread_id="t", target=target, input_ids=("a", "b"))
    prepared = SimpleNamespace(identity=SimpleNamespace(turn_id="turn"), cancel_event=asyncio.Event())
    monkeypatch.setattr("ws.local_send_load._local_readiness", lambda _target: Readiness("fits_now"))
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
async def test_out_of_scope_readiness_runs_without_load(monkeypatch):
    from ws.local_send_load import load_for_person as _load_for_person

    request = SimpleNamespace(thread_id="t", target=SimpleNamespace(catalog_id="omlx@srv/m"), input_ids=("a",))
    prepared = SimpleNamespace(identity=SimpleNamespace(turn_id="turn"), cancel_event=asyncio.Event())
    monkeypatch.setattr("ws.local_send_load._local_readiness", lambda _target: None)
    load = AsyncMock()
    monkeypatch.setattr("llm.local_model_load.ensure_loaded", load)
    await _load_for_person(request, prepared)
    load.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cause, expected", [("load_failed", "model_load_failed"), ("unreachable", "model_load_unreachable")])
async def test_load_failure_is_mapped(monkeypatch, cause, expected):
    from llm.local_model_load import ModelLoadFailed, Readiness
    from ws.local_send_load import load_for_person as _load_for_person

    target = SimpleNamespace(catalog_id="omlx@srv/m")
    request = SimpleNamespace(thread_id="t", target=target, input_ids=("a",))
    prepared = SimpleNamespace(identity=SimpleNamespace(turn_id="turn"), cancel_event=asyncio.Event())
    monkeypatch.setattr("ws.local_send_load._local_readiness", lambda _target: Readiness("fits_now"))
    monkeypatch.setattr("llm.local_model_load.ensure_loaded", AsyncMock(side_effect=ModelLoadFailed(cause)))
    monkeypatch.setattr("infra.state.broadcast_to_thread", Mock())
    with pytest.raises(RuntimeError, match=expected):
        await _load_for_person(request, prepared)


@pytest.mark.asyncio
async def test_stop_abandons_wait_without_cancelling_load(monkeypatch):
    from llm.local_model_load import Readiness
    from ws.local_send_load import load_for_person as _load_for_person

    target = SimpleNamespace(catalog_id="omlx@srv/m")
    request = SimpleNamespace(thread_id="t", target=target, input_ids=("a",))
    prepared = SimpleNamespace(identity=SimpleNamespace(turn_id="turn"), cancel_event=asyncio.Event())
    pending = asyncio.get_running_loop().create_future()
    started = asyncio.Event()

    async def load(_target):
        started.set()
        await pending

    monkeypatch.setattr("ws.local_send_load._local_readiness", lambda _target: Readiness("fits_now"))
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
