"""First successful External responses trigger a single model-specific image check."""
import asyncio

import pytest

from llm import external_image_input as image
from llm import external_provider, router
from llm.router import LLMResponse
from tests.test_external_image_input import _seed, _row, _SID


def _params(monkeypatch, dialect):
    from llm import network_discovery, network_endpoints
    monkeypatch.setattr(network_discovery, "get_server", lambda sid: {"id": sid, "dialect": dialect})
    monkeypatch.setattr(network_endpoints, "build_api_base", lambda sid: "http://example.test/v1")
    monkeypatch.setattr(external_provider, "resolve_server", lambda sid: None)
    return external_provider.build_external_params(_SID, "chosen")


@pytest.mark.parametrize("dialect", ["openai", "anthropic"])
def test_success_checks_only_chosen_model(isolated_db, monkeypatch, dialect):
    _seed(isolated_db, "chosen", "other")
    params = _params(monkeypatch, dialect)
    calls = []

    async def provider(messages, *args, **kwargs):
        is_probe = isinstance(messages[0]["content"], list)
        calls.append(is_probe)
        return LLMResponse(content="green" if is_probe else "hello")

    if dialect == "openai":
        monkeypatch.setattr("llm.base.base_completion", provider)
    else:
        monkeypatch.setattr("llm.providers.anthropic.anthropic_completion", provider)

    async def run():
        response = await router.chat_completion([{"role": "user", "content": "hi"}], **{k: v for k, v in params.items() if k != "uses_streaming"})
        assert response.content == "hello"
        await asyncio.gather(*list(image._PENDING))

    asyncio.run(run())
    assert calls == [False, True]
    assert _row(isolated_db, "chosen")[1]["result"] == "yes"
    assert _row(isolated_db, "other")[1] == {}


def test_metadata_only_declared_connection_then_first_use(isolated_db, monkeypatch):
    from api.network_server_helpers import add_external_server
    from api.network_servers import AddServerBody
    from llm import network_discovery
    from llm.external_catalog_view import external_catalog_models
    from omlx_provider_fixtures import _patch_httpx, _Router

    wire = _Router({})  # Model listing returns 404; only declared ids are available.
    _patch_httpx(monkeypatch, wire)
    monkeypatch.setattr(network_discovery, "discover_and_notify", lambda sid: asyncio.sleep(0))
    calls = []

    async def provider(messages, *args, **kwargs):
        probe = isinstance(messages[0]["content"], list)
        calls.append(probe)
        return LLMResponse(content="green" if probe else "hello")

    _install_provider(monkeypatch, "openai", provider)

    async def run():
        added = await add_external_server(AddServerBody(
            name="Declared", base_url="http://127.0.0.1:8000/v1", dialect="openai",
            model_ids=["chosen", "other"],
        ))
        sid = added["server_id"]
        assert (await external_provider.discover_external_models(sid))["ok"]
        assert calls == []
        params = external_provider.build_external_params(sid, "chosen")
        await _call(params)
        await asyncio.gather(*list(image._PENDING))
        models = {row["model_id"]: row for row in external_catalog_models(sid)["loaded"]}
        assert models["chosen"]["image_input"]["result"] == "yes"
        assert models["other"]["image_input"] is None

    asyncio.run(run())
    assert calls == [False, True]
    assert wire.calls and all(method == "GET" for method, _, _ in wire.calls)


def test_same_wire_model_on_another_server_is_untouched(isolated_db, monkeypatch):
    import json
    import sqlite3

    _seed(isolated_db, "chosen")
    with sqlite3.connect(str(isolated_db)) as db:
        db.execute(
            "INSERT INTO network_servers (id,provider,kind,dialect,instance_id,instance_name,port,status,last_seen_at,created_at,updated_at) "
            "VALUES ('other-server','external','external','openai','other','other',443,'online','t0','t0','t0')"
        )
    external_provider._reconcile_external_models(
        "other-server", {"id": "other-server", "instance_name": "other", "status": "online"},
        "openai", ["chosen"], {"supports_vision": 0, "supports_tools": 1, "supports_reasoning": 1},
        "t1", {"ok": False, "discovered": 0, "updated": 0, "missing": 0},
    )
    params = _params(monkeypatch, "openai")

    async def provider(messages, *args, **kwargs):
        return LLMResponse(content="green")

    _install_provider(monkeypatch, "openai", provider)

    async def run():
        await _call(params)
        await asyncio.gather(*list(image._PENDING))

    asyncio.run(run())
    with sqlite3.connect(str(isolated_db)) as db:
        (metadata,) = db.execute("SELECT metadata_json FROM models WHERE provider = ? AND provider_model_id = ?", ("external", "other-server/chosen")).fetchone()
    assert "image_input" not in json.loads(metadata or "{}")
    assert _row(isolated_db, "chosen")[1]["result"] == "yes"


def test_native_turn_forwards_identity_to_the_real_router(isolated_db, monkeypatch):
    from core.turn_provider_call import _StreamingCallbacks, _start_provider_call

    _seed(isolated_db, "chosen")
    params = _params(monkeypatch, "openai")
    calls = []

    async def provider(messages, *args, **kwargs):
        probe = isinstance(messages[0]["content"], list)
        calls.append(probe)
        if not probe:
            await kwargs["on_content_chunk"]("part")
        return LLMResponse(content="green" if probe else "hello")

    _install_provider(monkeypatch, "openai", provider)

    async def run():
        chunks = []

        async def on_step(kind, payload):
            chunks.append((kind, payload))

        callbacks = _StreamingCallbacks(on_step, "thread")
        task = _start_provider_call(
            None, [{"role": "user", "content": "hi"}], None,
            "external@ns-plan/chosen", params, None, None, None, "", None,
            None, params["model"], True, callbacks,
        )
        assert (await task).content == "hello"
        await asyncio.gather(*list(image._PENDING))
        assert chunks == [("text_delta", {"content": "part"})]

    asyncio.run(run())
    assert calls == [False, True]
    assert _row(isolated_db, "chosen")[1]["result"] == "yes"


def _call(params, **kwargs):
    return router.chat_completion(
        [{"role": "user", "content": "hi"}],
        **{k: v for k, v in params.items() if k != "uses_streaming"}, **kwargs,
    )


def _install_provider(monkeypatch, dialect, provider):
    if dialect == "openai":
        monkeypatch.setattr("llm.base.base_completion", provider)
    else:
        monkeypatch.setattr("llm.providers.anthropic.anthropic_completion", provider)


@pytest.mark.parametrize("dialect", ["openai", "anthropic"])
def test_concurrent_successes_share_one_claim_and_stream_callbacks(isolated_db, monkeypatch, dialect):
    from llm.router import ToolCallRequest

    _seed(isolated_db, "chosen")
    params = _params(monkeypatch, dialect)
    calls, chunks = [], []

    async def chunk(value):
        chunks.append(value)

    async def provider(messages, *args, **kw):
        probe = isinstance(messages[0]["content"], list)
        calls.append(probe)
        if probe:
            return LLMResponse(content="green")
        await kw["on_content_chunk"]("part")
        return LLMResponse(content=None, tool_calls=[ToolCallRequest("1", "tool", {})], finish_reason="tool_calls")

    _install_provider(monkeypatch, dialect, provider)

    async def run():
        replies = await asyncio.gather(*(_call(params, on_content_chunk=chunk) for _ in range(5)))
        await asyncio.gather(*list(image._PENDING))
        assert all(reply.has_tool_calls for reply in replies)

    asyncio.run(run())
    assert chunks == ["part"] * 5
    assert calls.count(True) == 1
    assert _row(isolated_db, "chosen")[1]["result"] == "yes"


@pytest.mark.parametrize("failure", ["response", "exception", "cancelled"])
def test_failed_or_cancelled_original_never_starts_check(isolated_db, monkeypatch, failure):
    _seed(isolated_db, "chosen")
    params = _params(monkeypatch, "openai")
    calls = []

    async def provider(messages, *args, **kwargs):
        calls.append(messages)
        if failure == "exception":
            raise RuntimeError("failed")
        if failure == "cancelled":
            raise asyncio.CancelledError()
        return LLMResponse(content="error", finish_reason="error")

    _install_provider(monkeypatch, "openai", provider)

    async def run():
        if failure == "response":
            assert (await _call(params)).finish_reason == "error"
        else:
            with pytest.raises(RuntimeError if failure == "exception" else asyncio.CancelledError):
                await _call(params)
        assert not image._PENDING

    asyncio.run(run())
    assert len(calls) == 1
    assert _row(isolated_db, "chosen")[1] == {}


@pytest.mark.parametrize("outcome", ["refused", "failure", "timeout"])
def test_inconclusive_automatic_check_is_unknown_and_not_retried(isolated_db, monkeypatch, outcome):
    _seed(isolated_db, "chosen")
    params = _params(monkeypatch, "openai")
    calls = []

    async def provider(messages, *args, **kwargs):
        probe = isinstance(messages[0]["content"], list)
        calls.append(probe)
        if not probe:
            return LLMResponse(content="hello")
        if outcome == "timeout":
            await asyncio.Event().wait()
        return LLMResponse(content="image unsupported" if outcome == "refused" else "unavailable", finish_reason="error")

    monkeypatch.setattr(image, "_PROBE_TIMEOUT_S", 0.01)
    _install_provider(monkeypatch, "openai", provider)

    async def run():
        await _call(params)
        await asyncio.gather(*list(image._PENDING))
        await _call(params)
        await asyncio.gather(*list(image._PENDING))

    asyncio.run(run())
    assert calls == [False, True, False]
    assert _row(isolated_db, "chosen")[1]["result"] == "unknown"


@pytest.mark.parametrize("when", ["before", "during"])
def test_user_choice_wins_over_automatic_check(isolated_db, monkeypatch, when):
    _seed(isolated_db, "chosen")
    params = _params(monkeypatch, "openai")
    calls = []

    async def run():
        started, finish = asyncio.Event(), asyncio.Event()

        async def provider(messages, *args, **kwargs):
            probe = isinstance(messages[0]["content"], list)
            calls.append(probe)
            if probe:
                started.set()
                await finish.wait()
                return LLMResponse(content="green")
            return LLMResponse(content="hello")

        _install_provider(monkeypatch, "openai", provider)
        if when == "before":
            await image.set_image_input(_SID, "chosen", "no")
        await _call(params)
        if when == "during":
            await asyncio.wait_for(started.wait(), 3)
            await image.set_image_input(_SID, "chosen", "no")
            finish.set()
        await asyncio.gather(*list(image._PENDING))

    asyncio.run(run())
    assert calls == ([False] if when == "before" else [False, True])
    assert _row(isolated_db, "chosen")[1]["source"] == "user"
    assert _row(isolated_db, "chosen")[0] == 0
