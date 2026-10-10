from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from aiohttp import ClientSession, web

# benchmarks/ sits at the repository root, outside backend/. The L3 job runs
# pytest from backend/, where the root is not on sys.path.
sys.path.append(str(Path(__file__).resolve().parents[2]))

from benchmarks.harbor.meter import JsonlMeter, harbor_totals  # noqa: E402
from benchmarks.harbor.proxy import MeteringProxy  # noqa: E402


async def _upstream_app() -> web.AppRunner:
    async def chat(request: web.Request) -> web.Response:
        return web.json_response({"model": "m", "usage": {"prompt_tokens": 5, "completion_tokens": 1}})

    async def models(request: web.Request) -> web.Response:
        return web.json_response({"data": []})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    app.router.add_get("/v1/models", models)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    return runner


def test_proxy_charges_posts_records_usage_and_refuses_over_cap(tmp_path):
    async def scenario():
        upstream = await _upstream_app()
        upstream_port = upstream.addresses[0][1]
        meter = JsonlMeter(tmp_path / "r.jsonl", unit_id="u", call_cap=1)
        proxy = MeteringProxy(meter, f"http://127.0.0.1:{upstream_port}")
        await proxy.start()
        base = f"http://127.0.0.1:{proxy.port}"
        try:
            async with ClientSession() as client:
                async with client.get(f"{base}/v1/models") as resp:
                    assert resp.status == 200
                async with client.post(f"{base}/v1/chat/completions", json={}) as resp:
                    assert (await resp.json())["usage"]["prompt_tokens"] == 5
                async with client.post(f"{base}/v1/chat/completions", json={}) as resp:
                    return resp.status
        finally:
            await proxy.stop()
            await upstream.cleanup()

    assert asyncio.run(scenario()) == 429
    assert harbor_totals(tmp_path / "r.jsonl") == {
        "input": 5, "cache": 0, "output": 1, "calls": 1, "calls_without_usage": 0,
    }
