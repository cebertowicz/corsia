"""A person's send explicitly loads an unloaded oMLX model before chat."""

import json
import statistics
import time
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from tests.omlx_provider_fixtures import _patch_httpx


@pytest.mark.asyncio
@pytest.mark.parametrize("already_loaded", [False, True])
async def test_send_loads_before_chat_using_live_omlx_http(isolated_db, monkeypatch, already_loaded):
    import anthropic  # noqa: F401 - load SDK before configuring the transport
    from openai import AsyncOpenAI
    from llm import base
    from infra.database import get_db
    from infra.input_render import PreparedBatch
    from infra.thread_driver import admit_input
    from infra.send_target import ready_target
    from infra.thread_execution import execute_claimed_batch
    from llm.resolution import resolve_execution_target
    from tests.unavailable_model_support import mark_model_loaded, message_row, seed_model
    from ws import turn

    calls = []
    status_ms = []
    loaded = already_loaded

    def router(request):
        nonlocal loaded
        method, path = request.method, request.url.path
        calls.append((method, path))
        if path == "/v1/models/status":
            return httpx.Response(200, json={"models": [{"id": "m", "loaded": loaded}]})
        if path == "/admin/api/login":
            return httpx.Response(200, json={"success": True}, headers={"set-cookie": "omlx_admin_session=test; Path=/"})
        if path == "/admin/api/models/m/load":
            loaded = True
            return httpx.Response(200, json={"success": True})
        if path == "/v1/chat/completions":
            return httpx.Response(200, json={"id": "chat", "object": "chat.completion", "created": 0, "model": "m", "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
        return httpx.Response(404)

    _patch_httpx(monkeypatch, router)
    send = httpx.AsyncClient.send

    async def timed_send(client, request, *args, **kwargs):
        start = time.perf_counter()
        response = await send(client, request, *args, **kwargs)
        if request.url.path == "/v1/models/status":
            status_ms.append((time.perf_counter() - start) * 1000)
        return response

    monkeypatch.setattr(httpx.AsyncClient, "send", timed_send)
    monkeypatch.setattr("infra.key_store_servers.resolve_server", lambda *_: "test")
    seed_model("omlx@srv/m")
    mark_model_loaded(monkeypatch, "omlx@srv/m", loaded=False)
    db = get_db()
    db.execute("UPDATE models SET metadata_json=? WHERE id=?", (json.dumps({"size_bytes": 1}), "omlx@srv/m"))
    db.execute("INSERT INTO network_server_endpoints (server_id,host,kind,created_at,updated_at) VALUES ('srv','127.0.0.1','local','t0','t0')")
    db.execute("INSERT INTO threads (id,user_id,model,created_at,updated_at) VALUES ('t','u','omlx@srv/m','t0','t0')")
    db.commit()
    target = ready_target(resolve_execution_target("omlx@srv/m", db=db), db=db)
    batch = admit_input("t", role="user", content="hi", intent="apply_now", message_id="m", turn_id="turn", boot_epoch="boot", admitted_at="t1", delivery_options={"native_mode": "event"}, db=db).batch
    thread = dict(db.execute("SELECT * FROM threads WHERE id='t'").fetchone())
    db.close()
    assert batch is not None
    if already_loaded:
        from ws.local_send_load import ready_for_turn
        _, admission = await ready_for_turn(target, load_for=("m",))

    async def prepare(batch, *, thread_row, target):
        return PreparedBatch(thread_row, target, "native", False, "hi", None, [], None, None, None, {}, batch)

    sdk_client = AsyncOpenAI(api_key="test", base_url="http://127.0.0.1:8210/v1", http_client=httpx.AsyncClient(), max_retries=0)
    real_acompletion = base.acompletion

    async def completion(**kwargs):
        return await real_acompletion(**kwargs, client=sdk_client)

    monkeypatch.setattr(base, "acompletion", completion)

    async def chat(request, prepared):
        response = await base.base_completion(
            [{"role": "user", "content": "hi"}], model="openai/m", api_key="omlx",
            api_base="http://127.0.0.1:8210/v1", omlx_server_id="srv", retry_transient_errors=False,
        )
        if response.finish_reason != "error":
            await request.settle_delivery()
        prepared.unbind(prepared.identity)
        return False

    monkeypatch.setattr("infra.thread_execution._prepare_batch", prepare)
    monkeypatch.setattr("infra.thread_execution.persist_prepared_installed_context", lambda *_: None)
    monkeypatch.setattr("infra.thread_execution.record_dispatch_manifest", lambda *_: None)
    monkeypatch.setattr("core.agent.get_agent", AsyncMock(return_value=object()))
    monkeypatch.setattr(turn, "make_provider_params", lambda _: {})
    monkeypatch.setattr(turn, "native_provider_params", lambda *_: {})
    monkeypatch.setattr(turn, "validate_native_target", AsyncMock())
    monkeypatch.setattr(turn, "_run_prepared_turn", chat)
    monkeypatch.setattr(turn, "run_post_turn_tasks", AsyncMock())
    broadcasts = Mock()
    monkeypatch.setattr("infra.state.broadcast_to_thread", broadcasts)
    result = await execute_claimed_batch(batch, thread_row=thread, target=target, user={"id": "u"})
    await result.turn_task
    await sdk_client.close()
    load_calls = [(m, p) for m, p in calls if p == "/admin/api/models/m/load"]
    assert not any("unload" in p for _, p in calls)
    if already_loaded:
        assert ("POST", "/v1/chat/completions") in calls
        assert not load_calls
        assert not any(call.args[1].get("type") == "model_loading" for call in broadcasts.call_args_list)
        assert admission.kind == "run"
    else:
        assert load_calls == [("POST", "/admin/api/models/m/load")], calls
        assert calls.index(("GET", "/v1/models/status")) < calls.index(load_calls[0]) < calls.index(("POST", "/v1/chat/completions"))
    assert message_row("m")[0] == "delivered"
    print(f"stub status GET: n={len(status_ms)} p50={statistics.median(status_ms):.3f}ms max={max(status_ms):.3f}ms")
