"""Keyless library search routing and outcomes."""

import time
from unittest.mock import AsyncMock, Mock

import primp
import pytest

from infra import key_store
from tools import web, web_keyless_search as keyless, web_search_outcome as outcomes
from tools.web import WebSearchTool


@pytest.fixture
def store(isolated_db, tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)


@pytest.mark.asyncio
async def test_brave_key_precedes_ddgs(store, monkeypatch):
    key_store.set_value("web-search", "brave-key")
    brave = AsyncMock(return_value=outcomes.SearchOutcome(outcomes.WORKS, results=[{"title": "Brave"}]))
    ddgs = AsyncMock()
    monkeypatch.setattr(web, "run_search", brave)
    monkeypatch.setattr(web, "keyless_search", ddgs)
    assert "Brave" in await WebSearchTool().execute("query")
    brave.assert_awaited_once_with("brave-key", "query", 5)
    ddgs.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_key_uses_ddgs_and_normalizes_results(store, monkeypatch):
    search = Mock(return_value=[{"title": "Hit", "href": "https://example.test", "body": "Snippet"}])
    monkeypatch.setattr(keyless, "DDGS", Mock(return_value=Mock(text=search)))
    text = await WebSearchTool().execute("query", count=1)
    assert "1. Hit\n   https://example.test\n   Snippet" in text
    search.assert_called_once_with("query", max_results=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [[], keyless.DDGSException("No results found.")])
async def test_an_empty_result_answers_once_as_working(monkeypatch, empty):
    search = Mock(side_effect=[empty])
    monkeypatch.setattr(keyless, "DDGS", Mock(return_value=Mock(text=search)))
    result = await keyless.keyless_search("query")
    assert (result.kind, result.results, search.call_count) == (outcomes.WORKS, [], 1)


@pytest.mark.asyncio
async def test_ddgs_failure_returns_reason_without_exception_text(monkeypatch):
    monkeypatch.setattr(keyless, "DDGS", Mock(side_effect=keyless.DDGSException("private error")))
    result = await keyless.keyless_search("query")
    assert (result.kind, result.reason) == (outcomes.FAILED, outcomes.REASON_PROVIDER)
    assert "private error" not in repr(result)


@pytest.mark.asyncio
async def test_non_ddgs_error_is_not_reported_as_provider_failure(monkeypatch):
    monkeypatch.setattr(keyless, "DDGS", Mock(side_effect=TypeError("changed signature")))
    with pytest.raises(TypeError):
        await keyless.keyless_search("query")


@pytest.mark.asyncio
async def test_keyless_timeout_returns_timeout_reason(monkeypatch):
    monkeypatch.setattr(keyless, "TIMEOUT_SECONDS", 0.01)
    def slow_search(query, count):
        time.sleep(0.1)
        return []
    monkeypatch.setattr(keyless, "_search", slow_search)
    result = await keyless.keyless_search("query")
    assert (result.kind, result.reason) == (outcomes.FAILED, outcomes.REASON_TIMEOUT)


class _RefusedClient:
    error: Exception = ConnectionError("Connection refused (os error 61)")

    def __init__(self, **_):
        pass

    def __getattr__(self, _):  # header and cookie setup the engines do before a request
        return lambda *_, **__: None

    def request(self, *_, **__):
        raise self.error


class _SlowClient(_RefusedClient):
    error = primp.TimeoutError("request expired")


@pytest.mark.asyncio
async def test_unreachable_network_through_the_real_library_is_a_network_failure(monkeypatch):
    from ddgs import http_client

    monkeypatch.setattr(http_client.primp, "Client", _RefusedClient)
    result = await keyless.keyless_search("query")
    assert (result.kind, result.reason) == (outcomes.FAILED, outcomes.REASON_NETWORK)


@pytest.mark.asyncio
async def test_an_engine_timeout_through_the_real_library_is_a_timeout(monkeypatch):
    """DDGS wraps the engine's timeout in a plain exception unless its text says 'timed out'."""
    from ddgs import http_client

    monkeypatch.setattr(http_client.primp, "Client", _SlowClient)
    result = await keyless.keyless_search("query")
    assert (result.kind, result.reason) == (outcomes.FAILED, outcomes.REASON_TIMEOUT)


@pytest.mark.asyncio
async def test_engines_finding_nothing_through_the_real_library_is_working_and_empty(monkeypatch):
    from ddgs.base import BaseSearchEngine

    monkeypatch.setattr(BaseSearchEngine, "search", lambda self, query, **_: [])
    result = await keyless.keyless_search("query")
    assert (result.kind, result.results) == (outcomes.WORKS, [])


@pytest.mark.asyncio
@pytest.mark.parametrize("error,reason", [
    (lambda: keyless.TimeoutException("timed out"), outcomes.REASON_TIMEOUT),
    (lambda: keyless.DDGSException("unexpected page"), outcomes.REASON_PROVIDER),
])
async def test_ddgs_failures_keep_their_class(monkeypatch, error, reason):
    search = Mock(side_effect=error())
    monkeypatch.setattr(keyless, "DDGS", Mock(return_value=Mock(text=search)))
    result = await keyless.keyless_search("query")
    assert (result.kind, result.reason) == (outcomes.FAILED, reason)
    assert search.call_count == 1


def test_first_run_posts_no_keyless_search_line_and_brave_only_after_a_key():
    from ddgs.engines import ENGINES

    from infra.system_package import system_resource_source
    from onboarding.first_run_wake import APP_REFERENCE, FIRST_RUN_REFERENCE

    for reference in (FIRST_RUN_REFERENCE, APP_REFERENCE):
        assert "Searches without a key go to free public sources" not in system_resource_source(reference).read_text()
    assert "Web search works. Searches go to Brave." in system_resource_source(APP_REFERENCE).read_text()
    assert {"duckduckgo", "yahoo", "wikipedia"} <= set(ENGINES["text"])
