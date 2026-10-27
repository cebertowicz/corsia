"""Keyless library search routing and outcomes."""

import time
from unittest.mock import AsyncMock, Mock

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
async def test_empty_twice_then_results(monkeypatch):
    search = Mock(side_effect=[[], [], [{"title": "Hit", "href": "https://example.test", "body": "Found"}]])
    monkeypatch.setattr(keyless, "DDGS", Mock(return_value=Mock(text=search)))
    result = await keyless.keyless_search("query")
    assert result.kind == outcomes.WORKS
    assert result.results == [{"title": "Hit", "url": "https://example.test", "description": "Found"}]
    assert search.call_count == 3


@pytest.mark.asyncio
async def test_ddgs_no_results_exception_is_retried(monkeypatch):
    search = Mock(side_effect=[keyless.DDGSException("No results found."), [], [{"title": "Hit"}]])
    monkeypatch.setattr(keyless, "DDGS", Mock(return_value=Mock(text=search)))
    result = await keyless.keyless_search("query")
    assert result.kind == outcomes.WORKS
    assert result.results[0]["title"] == "Hit"
    assert search.call_count == 3


@pytest.mark.asyncio
async def test_empty_after_three_attempts(monkeypatch):
    search = Mock(return_value=[])
    monkeypatch.setattr(keyless, "DDGS", Mock(return_value=Mock(text=search)))
    result = await keyless.keyless_search("query")
    assert (result.kind, result.results, search.call_count) == (outcomes.WORKS, [], 3)


@pytest.mark.asyncio
async def test_ddgs_failure_returns_reason_without_exception_text(monkeypatch):
    monkeypatch.setattr(keyless, "DDGS", Mock(side_effect=RuntimeError("private error")))
    result = await keyless.keyless_search("query")
    assert (result.kind, result.reason) == (outcomes.FAILED, outcomes.REASON_PROVIDER)
    assert "private error" not in repr(result)


@pytest.mark.asyncio
async def test_keyless_timeout_returns_timeout_reason(monkeypatch):
    monkeypatch.setattr(keyless, "TIMEOUT_SECONDS", 0.01)
    def slow_search(query, count):
        time.sleep(0.1)
        return []
    monkeypatch.setattr(keyless, "_search", slow_search)
    result = await keyless.keyless_search("query")
    assert (result.kind, result.reason) == (outcomes.FAILED, outcomes.REASON_TIMEOUT)


def _client_failure() -> keyless.DDGSException:
    try:
        raise keyless.DDGSException("ConnectError") from OSError("connection refused")
    except keyless.DDGSException as exc:
        return exc


@pytest.mark.asyncio
@pytest.mark.parametrize("error,reason", [
    (lambda: keyless.RatelimitException("202 Ratelimit"), outcomes.REASON_RATE_LIMIT),
    (lambda: keyless.TimeoutException("timed out"), outcomes.REASON_TIMEOUT),
    (_client_failure, outcomes.REASON_NETWORK),
    (lambda: keyless.DDGSException("unexpected page"), outcomes.REASON_PROVIDER),
])
async def test_ddgs_failures_keep_their_class(monkeypatch, error, reason):
    search = Mock(side_effect=error())
    monkeypatch.setattr(keyless, "DDGS", Mock(return_value=Mock(text=search)))
    result = await keyless.keyless_search("query")
    assert (result.kind, result.reason) == (outcomes.FAILED, reason)
    assert search.call_count == 1
