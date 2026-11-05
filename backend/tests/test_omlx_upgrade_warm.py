"""Both inference producers participate in the managed replacement boundary."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from llm.providers import omlx_lifecycle, omlx_restart, omlx_supervisor


@pytest.fixture
def warm_seat(monkeypatch):
    life = omlx_restart.Life()
    monkeypatch.setattr(omlx_supervisor, "_life", {"seat": life})
    monkeypatch.setattr(omlx_lifecycle, "_api_base", lambda _: "http://warm.invalid/v1")
    loaded = AsyncMock(return_value=[{"identifier": "same-model"}])
    monkeypatch.setattr(omlx_lifecycle, "list_loaded", loaded)
    monkeypatch.setattr(omlx_lifecycle, "public_headers", AsyncMock(return_value={}))
    entered, finish = asyncio.Event(), asyncio.Event()

    async def respond(request):
        entered.set()
        await finish.wait()
        return httpx.Response(200, json={"usage": {"prompt_tokens": 42}})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        omlx_lifecycle.httpx, "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs),
    )
    return life, loaded, entered, finish


async def test_admitted_warm_finishes_before_upgrade_drain(warm_seat):
    life, _, entered, finish = warm_seat
    task = asyncio.create_task(omlx_lifecycle.warm_cache({"server_id": "seat"}, "same-model", []))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert len(life.requests) == 1, "the upgrade must account for an in-flight warm"
        drain = asyncio.create_task(asyncio.wait(tuple(life.requests)))
        await asyncio.sleep(0)
        assert not drain.done()
        finish.set()
        assert (await task)["ok"] is True
        await asyncio.wait_for(drain, 1)
        assert not life.requests
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_warm_waits_before_loaded_proof_until_restoration(warm_seat):
    life, loaded, entered, finish = warm_seat
    restore = asyncio.Event()

    async def replace():
        try:
            await restore.wait()
        finally:
            life.hold_requests = False
            life.restart = None

    replacement = asyncio.create_task(replace())
    life.restart = replacement
    life.hold_requests = True
    task = asyncio.create_task(omlx_lifecycle.warm_cache({"server_id": "seat"}, "same-model", []))
    try:
        await asyncio.sleep(0)
        loaded.assert_not_awaited()
        assert not entered.is_set()
        restore.set()
        await asyncio.wait_for(entered.wait(), 1)
        loaded.assert_awaited_once()
        finish.set()
        assert (await task)["ok"] is True
    finally:
        restore.set()
        finish.set()
        await asyncio.gather(replacement, task, return_exceptions=True)
