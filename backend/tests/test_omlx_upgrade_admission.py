"""Automatic replacement cannot cut off an admitted answer or race a new provider call."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from llm import base
from llm.providers import omlx_lifecycle, omlx_supervisor
from llm.router import LLMResponse
from test_omlx_upgrade import _seat, _upgrade


def _transport(monkeypatch):
    entered, finish = asyncio.Event(), asyncio.Event()

    async def stream(*_args, **_kwargs):
        entered.set()
        await finish.wait()
        return LLMResponse(content="answered")

    monkeypatch.setattr(base, "_omlx_loaded_only_preflight", AsyncMock(return_value=None))
    monkeypatch.setattr("llm.providers.omlx_public_auth.public_headers", AsyncMock(return_value={}))
    monkeypatch.setattr(base, "_streaming_completion", stream)
    return entered, finish


async def _call(server_id="seat"):
    return await base.base_completion(
        [{"role": "user", "content": "answer"}], model="openai/exact-model",
        api_key="omlx", omlx_server_id=server_id, on_content_chunk=lambda _chunk: None,
    )


async def _cancel(*tasks):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_request_during_model_snapshot_waits_through_restore(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="old")
    entered, finish = _transport(monkeypatch)
    snapshot, release_snapshot = asyncio.Event(), asyncio.Event()
    restoring, release_restore = asyncio.Event(), asyncio.Event()

    async def loaded(_cfg):
        snapshot.set()
        await release_snapshot.wait()
        return [{"identifier": "exact-model"}]

    async def restore(*_args):
        restoring.set()
        await release_restore.wait()

    monkeypatch.setattr(omlx_lifecycle, "read_loaded", loaded)
    monkeypatch.setattr("llm.local_keep_loaded.reload_after_restart", restore)
    upgrade = asyncio.create_task(_upgrade())
    await asyncio.wait_for(snapshot.wait(), 2)
    request = asyncio.create_task(_call())
    try:
        # A round-trip through the event loop lets the request reach admission.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not entered.is_set(), "request entered after idle proof during loaded-model HTTP"
        release_snapshot.set()
        await asyncio.wait_for(restoring.wait(), 2)
        assert not entered.is_set(), "request entered before exact models were restored"
        release_restore.set()
        assert (await asyncio.wait_for(upgrade, 2))["restarted"] is True
        await asyncio.wait_for(entered.wait(), 2)
        finish.set()
        assert (await asyncio.wait_for(request, 2)).content == "answered"
        assert [call[0] for call in calls] == ["stop", "start"]
    finally:
        await _cancel(request, upgrade)


async def test_request_that_beat_restart_finishes_before_stop(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="old")
    entered, finish = _transport(monkeypatch)
    request = asyncio.create_task(_call())
    await asyncio.wait_for(entered.wait(), 2)
    upgrade = asyncio.create_task(_upgrade())
    try:
        # Wait for the existing restart owner to claim replacement, without
        # assuming a particular number of DB-worker event-loop turns.
        async with asyncio.timeout(2):
            while not omlx_supervisor.is_restarting("seat") and not upgrade.done():
                await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not any(call[0] == "stop" for call in calls), "upgrade stopped a live answer"
        finish.set()
        assert (await asyncio.wait_for(request, 2)).content == "answered"
        assert (await asyncio.wait_for(upgrade, 2))["restarted"] is True
    finally:
        await _cancel(request, upgrade)


async def test_cancelling_an_active_request_releases_upgrade(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="old")
    entered, _ = _transport(monkeypatch)
    request = asyncio.create_task(_call())
    await asyncio.wait_for(entered.wait(), 2)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert (await asyncio.wait_for(_upgrade(), 2))["restarted"] is True
    assert any(call[0] == "stop" for call in calls)


async def test_cancelling_a_waiter_does_not_cancel_restart(isolated_db, monkeypatch):
    _seat(monkeypatch, recorded="old")
    entered, _ = _transport(monkeypatch)
    snapshot, release = asyncio.Event(), asyncio.Event()

    async def loaded(_cfg):
        snapshot.set()
        await release.wait()
        return []

    monkeypatch.setattr(omlx_lifecycle, "read_loaded", loaded)
    upgrade = asyncio.create_task(_upgrade())
    await asyncio.wait_for(snapshot.wait(), 2)
    request = asyncio.create_task(_call())
    try:
        await asyncio.sleep(0)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert not entered.is_set()
        assert not upgrade.done()
        release.set()
        assert (await asyncio.wait_for(upgrade, 2))["restarted"] is True
    finally:
        await _cancel(request, upgrade)


async def test_parallel_calls_are_not_serialized(isolated_db, monkeypatch):
    _seat(monkeypatch, recorded="old")
    both_entered, finish = asyncio.Event(), asyncio.Event()
    entered = []

    async def stream(*_args, **_kwargs):
        entered.append(True)
        if len(entered) == 2:
            both_entered.set()
        await finish.wait()
        return LLMResponse(content="answered")

    _transport(monkeypatch)
    monkeypatch.setattr(base, "_streaming_completion", stream)
    requests = [asyncio.create_task(_call()) for _ in range(2)]
    try:
        await asyncio.wait_for(both_entered.wait(), 2)
        finish.set()
        assert all(reply.content == "answered" for reply in await asyncio.gather(*requests))
    finally:
        await _cancel(*requests)


async def test_other_server_is_not_held_by_managed_restart(isolated_db, monkeypatch):
    _seat(monkeypatch, recorded="old")
    entered, finish = _transport(monkeypatch)
    snapshot, release = asyncio.Event(), asyncio.Event()

    async def loaded(_cfg):
        snapshot.set()
        await release.wait()
        return []

    monkeypatch.setattr(omlx_lifecycle, "read_loaded", loaded)
    upgrade = asyncio.create_task(_upgrade())
    await asyncio.wait_for(snapshot.wait(), 2)
    request = asyncio.create_task(_call("other"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        finish.set()
        assert (await asyncio.wait_for(request, 2)).content == "answered"
        release.set()
        await asyncio.wait_for(upgrade, 2)
    finally:
        await _cancel(request, upgrade)
