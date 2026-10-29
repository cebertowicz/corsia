"""The oMLX warm probe sees the same tool history as a live send."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_gemma_warm_probe_matches_live_wire(monkeypatch):
    from api import thread_cache_warm as warm
    from llm import base, router

    selector = "omlx@server/gemma"
    history = [
        {"role": "user", "content": "Look it up"},
        {"role": "assistant", "content": "Checking", "tool_calls": [
            {"id": "call1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call1", "content": "Found"},
    ]
    monkeypatch.setattr("llm.catalog.get_model_by_litellm", lambda key: {
        "litellm_model": selector, "metadata_json": json.dumps({"config_model_type": "gemma4"}),
    } if key == selector else None)
    agent = SimpleNamespace(_connect_mcp=AsyncMock(), tools=[])
    monkeypatch.setattr("core.agent.get_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr("tools.confined_reads.advertised", AsyncMock(return_value=[]))
    monkeypatch.setattr(warm, "run_in_db", AsyncMock(return_value=(None, None, None)))
    monkeypatch.setattr(warm, "_render_warm_replay", AsyncMock(return_value=(history, {
        "history_messages": 3, "history_tokens": 1, "was_trimmed": False,
    })))
    monkeypatch.setattr("llm.reasoning_config.reasoning_config_for_model", lambda _: None)
    target = SimpleNamespace(catalog_id=selector, transport_model=selector,
                             preserve_thinking_default=False, provider="omlx")
    messages, tools, shape = await warm._build_warm_prefix("thread", None, target, None)

    wire = []
    async def complete(kwargs, **_):
        wire.append(kwargs["messages"])
        return router.LLMResponse(content="ok")
    async def observed(_base, dispatch):
        return await dispatch
    async def mark(response, _server):
        return response
    monkeypatch.setattr(base, "_completion_with_retries", complete)
    monkeypatch.setattr(base, "_omlx_loaded_only_preflight", AsyncMock(return_value=None))
    monkeypatch.setattr("llm.providers.omlx_public_auth.public_headers", AsyncMock(return_value={}))
    monkeypatch.setattr(router, "_observed", observed)
    monkeypatch.setattr(router, "mark_own_server", mark)
    await router.chat_completion(history, model="openai/gemma", api_key="omlx",
                                 api_base="http://example.test/v1", omlx_server_id="server")

    probe = AsyncMock(return_value={"ok": True, "total_blocks": 1, "block_size": 1,
                                    "total_tokens": 1, "blocks_ssd_hot": 1})
    monkeypatch.setattr("llm.providers.for_provider", lambda _: SimpleNamespace(probe_cache=probe))
    result = await warm._submit_warm("thread", target, messages, tools, shape)
    assert result["message"] == "Already warm"
    assert messages == wire[0] == probe.await_args.kwargs["messages"]
    assert messages[1]["content"] is None
    assert history[1]["content"] == "Checking"
