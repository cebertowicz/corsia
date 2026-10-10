from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest


def _completion_response():
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="ok", tool_calls=None),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


def _install_status_transport(monkeypatch, response: httpx.Response) -> list[str]:
    import llm.providers.omlx_lifecycle as lifecycle

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return response

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(lifecycle.httpx, "AsyncClient", client)
    return calls


@pytest.mark.asyncio
async def test_unloaded_omlx_model_never_reaches_chat_completion(monkeypatch):
    from llm.router import chat_completion

    calls = _install_status_transport(
        monkeypatch,
        httpx.Response(
            200,
            json={"models": [{"id": "Fortissimo", "loaded": False}]},
        ),
    )
    with patch("llm.base.acompletion", new_callable=AsyncMock) as completion:
        completion.return_value = _completion_response()
        response = await chat_completion(
            [{"role": "user", "content": "checkpoint"}],
            model="openai/Fortissimo",
            api_key="omlx",
            api_base="http://omlx.test/v1",
        )

    assert response.finish_reason == "error"
    assert "loaded" in str(response.content).lower()
    completion.assert_not_awaited()
    assert calls == ["http://omlx.test/v1/models/status"]


@pytest.mark.asyncio
async def test_unknown_omlx_loaded_state_fails_closed_without_retry(monkeypatch):
    from llm.router import chat_completion

    calls = _install_status_transport(monkeypatch, httpx.Response(503))
    with patch("llm.base.acompletion", new_callable=AsyncMock) as completion:
        completion.return_value = _completion_response()
        response = await chat_completion(
            [{"role": "user", "content": "ordinary turn"}],
            model="openai/Fortissimo",
            api_key="omlx",
            api_base="http://omlx.test/v1",
        )

    assert response.finish_reason == "error"
    completion.assert_not_awaited()
    assert calls == ["http://omlx.test/v1/models/status"]


@pytest.mark.asyncio
async def test_loaded_omlx_model_reaches_chat_completion(monkeypatch):
    from llm.router import chat_completion

    calls = _install_status_transport(
        monkeypatch,
        httpx.Response(
            200,
            json={"models": [{"id": "Fortissimo", "loaded": True}]},
        ),
    )
    with patch("llm.base.acompletion", new_callable=AsyncMock) as completion:
        completion.return_value = _completion_response()
        response = await chat_completion(
            [{"role": "user", "content": "ordinary turn"}],
            model="openai/Fortissimo",
            api_key="omlx",
            api_base="http://omlx.test/v1",
        )

    assert response.content == "ok"
    completion.assert_awaited_once()
    assert calls == ["http://omlx.test/v1/models/status"]
