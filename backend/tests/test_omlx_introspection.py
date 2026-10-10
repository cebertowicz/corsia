
from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx
import pytest

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm.providers.omlx_introspection import (  # noqa: E402
    _CANONICAL_SAMPLING_FIELDS,
    _FALLBACK_KNOWN_DROPPED,
    _extract_chat_completion_fields,
    _parse_host_port,
    accepted_fields_cached,
    invalidate_cache,
    silently_dropped_cached,
    warm_cache,
)




class TestParseHostPort:
    def test_typical_omlx_base(self):
        assert _parse_host_port("http://localhost:5200/v1") == ("localhost", 5200)

    def test_with_hostname(self):
        assert _parse_host_port("http://Jarviss-Mac-Studio.local:5200/v1") == (
            "jarviss-mac-studio.local",  # urlparse lowercases hostname
            5200,
        )

    def test_no_port(self):
        assert _parse_host_port("http://localhost/v1") == ("localhost", None)

    def test_garbage(self):
        host, port = _parse_host_port("not a url")
        assert port is None  # what matters: the caller treats None as "skip".


class TestExtractChatCompletionFields:
    def test_happy_path(self):
        openapi = {
            "components": {
                "schemas": {
                    "ChatCompletionRequest": {
                        "properties": {
                            "model": {"type": "string"},
                            "temperature": {},
                            "top_p": {},
                        },
                    }
                }
            }
        }
        result = _extract_chat_completion_fields(openapi)
        assert result == frozenset({"model", "temperature", "top_p"})

    def test_missing_components(self):
        assert _extract_chat_completion_fields({}) is None

    def test_missing_chat_completion_request(self):
        openapi = {"components": {"schemas": {"OtherThing": {"properties": {}}}}}
        assert _extract_chat_completion_fields(openapi) is None

    def test_empty_properties(self):
        openapi = {
            "components": {"schemas": {"ChatCompletionRequest": {"properties": {}}}}
        }
        assert _extract_chat_completion_fields(openapi) is None




@pytest.fixture(autouse=True)
def _clear_cache():
    invalidate_cache()
    yield
    invalidate_cache()


class TestCacheLifecycle:
    def test_cold_cache_returns_none(self):
        assert accepted_fields_cached("http://localhost:9999/v1") is None

    def test_silently_dropped_cold_returns_static_fallback(self):
        from llm.providers.omlx_introspection import _ADMIN_HONORED_FIELDS
        result = silently_dropped_cached("http://localhost:9999/v1")
        assert result == _FALLBACK_KNOWN_DROPPED - _ADMIN_HONORED_FIELDS

    def test_invalidate_specific_url(self):
        from llm.providers.omlx_introspection import _CACHE
        import time
        _CACHE[("a", 1)] = (time.monotonic(), frozenset({"temperature"}))
        _CACHE[("b", 2)] = (time.monotonic(), frozenset({"top_p"}))
        invalidate_cache("http://a:1/v1")
        assert ("a", 1) not in _CACHE
        assert ("b", 2) in _CACHE

    def test_invalidate_all(self):
        from llm.providers.omlx_introspection import _CACHE
        import time
        _CACHE[("a", 1)] = (time.monotonic(), frozenset({"temperature"}))
        _CACHE[("b", 2)] = (time.monotonic(), frozenset({"top_p"}))
        invalidate_cache()
        assert _CACHE == {}

    def test_ttl_expiry(self):
        from llm.providers.omlx_introspection import _CACHE, _CACHE_TTL_SECONDS
        import time
        _CACHE[("x", 5200)] = (
            time.monotonic() - _CACHE_TTL_SECONDS - 1,
            frozenset({"temperature"}),
        )
        assert accepted_fields_cached("http://x:5200/v1") is None




def _make_openapi(extra_fields: list[str] | None = None) -> dict:
    fields = ["model", "messages", "temperature", "top_p", "max_tokens"]
    if extra_fields:
        fields = fields + extra_fields
    return {
        "components": {
            "schemas": {
                "ChatCompletionRequest": {
                    "properties": {f: {} for f in fields},
                }
            }
        }
    }


class _MockTransport(httpx.AsyncBaseTransport):

    def __init__(self, responses: dict[str, httpx.Response]):
        self.responses = responses
        self.calls: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        resp = self.responses.get(url)
        if resp is None:
            raise httpx.ConnectError(f"no canned response for {url}")
        return resp


def _patched_async_client(transport: httpx.AsyncBaseTransport):
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    return patch("llm.providers.omlx_introspection.httpx.AsyncClient", factory)


@pytest.mark.asyncio
async def test_warm_cache_happy_path():
    transport = _MockTransport({
        "http://localhost:5200/openapi.json": httpx.Response(
            200, json=_make_openapi(),
        ),
    })
    with _patched_async_client(transport):
        fields = await warm_cache("http://localhost:5200/v1")
    assert fields == frozenset({"model", "messages", "temperature", "top_p", "max_tokens"})
    assert accepted_fields_cached("http://localhost:5200/v1") == fields


@pytest.mark.asyncio
async def test_warm_cache_skips_when_already_warm():
    transport = _MockTransport({
        "http://localhost:5200/openapi.json": httpx.Response(
            200, json=_make_openapi(),
        ),
    })
    with _patched_async_client(transport):
        await warm_cache("http://localhost:5200/v1")
        await warm_cache("http://localhost:5200/v1")
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_warm_cache_transport_failure_returns_none():
    transport = _MockTransport({})  # no canned responses → ConnectError
    with _patched_async_client(transport):
        result = await warm_cache("http://localhost:5200/v1")
    assert result is None
    assert accepted_fields_cached("http://localhost:5200/v1") is None


@pytest.mark.asyncio
async def test_warm_cache_malformed_payload_returns_none():
    transport = _MockTransport({
        "http://localhost:5200/openapi.json": httpx.Response(
            200, json={"components": {"schemas": {}}},  # no ChatCompletionRequest
        ),
    })
    with _patched_async_client(transport):
        result = await warm_cache("http://localhost:5200/v1")
    assert result is None


@pytest.mark.asyncio
async def test_warm_cache_http_error_returns_none():
    transport = _MockTransport({
        "http://localhost:5200/openapi.json": httpx.Response(500, text="oops"),
    })
    with _patched_async_client(transport):
        result = await warm_cache("http://localhost:5200/v1")
    assert result is None




@pytest.mark.asyncio
async def test_silently_dropped_after_warm_returns_live_gap():
    from llm.providers.omlx_introspection import _ADMIN_HONORED_FIELDS
    transport = _MockTransport({
        "http://localhost:5200/openapi.json": httpx.Response(
            200, json=_make_openapi(),  # default: just basic fields
        ),
    })
    with _patched_async_client(transport):
        await warm_cache("http://localhost:5200/v1")
    dropped = silently_dropped_cached("http://localhost:5200/v1")
    expected = (
        _CANONICAL_SAMPLING_FIELDS
        - frozenset({"temperature", "top_p", "max_tokens"})
    ) - _ADMIN_HONORED_FIELDS
    assert dropped == expected


@pytest.mark.asyncio
async def test_silently_dropped_full_schema_returns_empty():
    transport = _MockTransport({
        "http://localhost:5200/openapi.json": httpx.Response(
            200, json=_make_openapi(extra_fields=list(_CANONICAL_SAMPLING_FIELDS)),
        ),
    })
    with _patched_async_client(transport):
        await warm_cache("http://localhost:5200/v1")
    dropped = silently_dropped_cached("http://localhost:5200/v1")
    assert dropped == frozenset()
