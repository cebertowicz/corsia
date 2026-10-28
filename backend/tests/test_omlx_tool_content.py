"""Tool-call content is shaped only at the provider boundary."""

import json

import pytest

from llm.base import sanitize_empty_content
from llm.catalog_network_models import _network_model_payload, _parse_omlx_status
from llm.omlx_capabilities import apply_admin_model
from llm.providers.omlx_messages import shape_messages


CALL = {"role": "assistant", "content": "Before the call", "tool_calls": [{"id": "call_1"}]}


@pytest.mark.parametrize("content", ["", "\n", " \t\n"])
def test_assistant_tool_call_whitespace_becomes_null(content):
    original = [{**CALL, "content": content}, {"role": "user", "content": " "}]
    result = sanitize_empty_content(original)
    assert result[0]["content"] is None
    assert result[1]["content"] == " "
    assert original[0]["content"] == content


def test_discovered_family_survives_catalog_payload():
    class Response:
        status_code = 200

        def json(self):
            return {"models": [{"id": "renamed", "config_model_type": "gemma4"}]}

    canonical = _parse_omlx_status(Response())
    apply_admin_model(canonical, {"id": "renamed", "config_model_type": "gemma4_unified"})
    row = _network_model_payload("server", {}, "omlx", canonical["renamed"], {}, None)
    assert json.loads(row["metadata_json"])["config_model_type"] == "gemma4_unified"


@pytest.mark.parametrize("family", ["gemma4", "gemma4_unified"])
def test_gemma_shape_is_outgoing_copy(monkeypatch, family):
    selector = "omlx@server/renamed"
    monkeypatch.setattr("llm.catalog.get_model_by_litellm", lambda _: {
        "litellm_model": selector, "metadata_json": json.dumps({"config_model_type": family}),
    })
    messages = [CALL, {"role": "tool", "content": "result"}]
    shaped = shape_messages(messages, "openai/renamed", "server")
    assert shaped[0]["content"] is None
    assert messages[0]["content"] == "Before the call"
    assert shaped[1] == messages[1]


@pytest.mark.asyncio
async def test_other_provider_keeps_assistant_text(monkeypatch):
    from llm import base, router

    seen = []

    async def complete(messages, *args, **kwargs):
        seen.append(messages)
        return router.LLMResponse(content="ok")

    async def observed(_base, dispatch):
        return await dispatch

    async def mark(response, _server_id):
        return response

    monkeypatch.setattr(base, "base_completion", complete)
    monkeypatch.setattr(router, "_observed", observed)
    monkeypatch.setattr(router, "mark_own_server", mark)
    await router.chat_completion([CALL], model="openai/renamed", api_base="http://example.test/v1", api_key="other")
    assert seen[0][0]["content"] == "Before the call"


def test_other_model_and_unknown_metadata_are_unchanged(monkeypatch):
    for family in ("qwen4_exp", None):
        monkeypatch.setattr("llm.catalog.get_model_by_litellm", lambda _: {
            "litellm_model": "omlx@server/qwen", "metadata_json": json.dumps({"config_model_type": family}),
        })
        messages = [CALL]
        assert shape_messages(messages, "openai/qwen", "server") is messages
