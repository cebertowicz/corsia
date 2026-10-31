from __future__ import annotations

import asyncio

import httpx


def test_managed_sweep_stores_the_model_memory_oMLX_reports(isolated_db):
    """oMLX 0.7.1rc1 sends ``model_memory_used``/``model_memory_max`` in bytes."""
    from llm import network_discovery as nd

    server_id = nd.upsert_server("omlx", "m", "m", "127.0.0.1", 5200, "offline", kind="managed")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json={
            "status": "ok", "loaded_models": ["m1"],
            "model_memory_used": 19683470507, "model_memory_max": 24219694464,
        })

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await nd._sweep_managed(client, nd.get_server(server_id))

    asyncio.run(run())
    server = nd.get_server(server_id)
    assert server["memory_used_mb"] == 19683470507 // 2 ** 20
    assert server["memory_total_mb"] == 24219694464 // 2 ** 20
