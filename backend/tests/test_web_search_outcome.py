"""One classifier decides what a search came to, and nothing it returns carries the key.

``web_search`` and Settings › Keys' Check both read it, so a refused key is
never reported as a network, timeout or quota failure, and the provider's error
text (which may echo the key) never reaches an agent or the person.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock

import httpx
import pytest

from infra import key_store
from tools import web as web_mod
from tools import web_search_outcome as outcome_mod
from tools.web import WebSearchTool
from tools.web_search_outcome import (
    FAILED,
    KEY_REFUSED,
    REASON_NETWORK,
    REASON_PROVIDER,
    REASON_RATE_LIMIT,
    REASON_TIMEOUT,
    WORKS,
    classify_status,
    run_search,
)

VALUE = "BSAoutcome-test-value-0123456789"
_REAL_CLIENT = httpx.AsyncClient


def _transport(monkeypatch, handler):
    """Send every search through ``handler`` instead of the network."""
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def client(*args, **kwargs):
        return _REAL_CLIENT(*args, transport=httpx.MockTransport(recording), **kwargs)

    monkeypatch.setattr(outcome_mod.httpx, "AsyncClient", client)
    return seen


def _answer(status: int, body: object):
    def handler(_request):
        # The provider's text echoes the key, as some error pages do.
        if isinstance(body, dict):
            return httpx.Response(status, json=body)
        return httpx.Response(status, text=f"{body} {VALUE}")

    return handler


@pytest.mark.parametrize("status,body,kind,reason", [
    (401, {"error": {"code": "UNAUTHORIZED"}}, KEY_REFUSED, None),
    (403, {}, KEY_REFUSED, None),
    (422, {"error": {"code": "SUBSCRIPTION_TOKEN_INVALID", "detail": VALUE}}, KEY_REFUSED, None),
    (422, {"error": {"code": "VALIDATION"}}, FAILED, REASON_PROVIDER),
    (404, {}, FAILED, REASON_PROVIDER),
    (429, {"error": {"code": "RATE_LIMITED"}}, FAILED, REASON_RATE_LIMIT),
    (500, "Internal error", FAILED, REASON_PROVIDER),
    (503, "unavailable", FAILED, REASON_PROVIDER),
    (200, "not json", FAILED, REASON_PROVIDER),
])
@pytest.mark.asyncio
async def test_provider_answers_are_classified(monkeypatch, status, body, kind, reason):
    seen = _transport(monkeypatch, _answer(status, body))
    outcome = await run_search(VALUE, "q", 1)
    assert (outcome.kind, outcome.reason) == (kind, reason)
    assert VALUE not in repr(outcome)
    # The key goes in the header only, never the URL.
    assert seen[0].headers["X-Subscription-Token"] == VALUE
    assert VALUE not in str(seen[0].url)


def test_a_key_error_code_is_refused_whatever_the_status():
    assert classify_status(400, {"code": "INVALID_API_KEY"}).kind == KEY_REFUSED
    assert classify_status(500, {"error": {"code": "SUBSCRIPTION_EXPIRED"}}).kind == KEY_REFUSED
    assert classify_status(200, {"web": {}}) is None


@pytest.mark.asyncio
async def test_results_and_zero_results_both_work(monkeypatch):
    _transport(monkeypatch, _answer(200, {"web": {"results": [{"title": "T", "url": "u"}]}}))
    found = await run_search(VALUE, "q", 1)
    assert (found.kind, found.results) == (WORKS, [{"title": "T", "url": "u"}])
    _transport(monkeypatch, _answer(200, {"query": {}}))
    empty = await run_search(VALUE, "q", 1)
    assert (empty.kind, empty.results) == (WORKS, [])


@pytest.mark.parametrize("raised,reason", [
    (httpx.ConnectError(f"cannot connect {VALUE}"), REASON_NETWORK),
    (httpx.ReadTimeout(f"timed out {VALUE}"), REASON_TIMEOUT),
    (RuntimeError(f"surprise {VALUE}"), REASON_PROVIDER),
])
@pytest.mark.asyncio
async def test_transport_failures_keep_no_exception_text(monkeypatch, raised, reason):
    def handler(_request):
        raise raised

    _transport(monkeypatch, handler)
    outcome = await run_search(VALUE, "q", 1)
    assert (outcome.kind, outcome.reason) == (FAILED, reason)
    assert VALUE not in repr(outcome)


@pytest.fixture
def store(isolated_db, tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)


@pytest.mark.parametrize("status,body,first_line", [
    (401, {"message": VALUE}, "Web search: key refused."),
    (422, {"error": {"code": "SUBSCRIPTION_TOKEN_INVALID", "detail": VALUE}}, "Web search: key refused."),
    (429, {"message": VALUE}, "Web search failed: rate limit or quota."),
    (502, "bad gateway", "Web search failed: provider error."),
])
@pytest.mark.asyncio
async def test_web_search_errors_name_the_class_and_never_the_key(store, monkeypatch, caplog, status, body, first_line):
    key_store.set_value("web-search", VALUE)
    _transport(monkeypatch, _answer(status, body))
    with caplog.at_level(logging.DEBUG):
        text = await WebSearchTool().execute("q")
    assert text.splitlines()[0] == first_line
    assert VALUE not in text
    assert VALUE not in caplog.text


@pytest.mark.asyncio
async def test_a_newly_saved_key_is_used_on_the_next_call(store, monkeypatch):
    seen = _transport(monkeypatch, _answer(200, {"web": {"results": [{"title": "Hit", "url": "https://x"}]}}))
    monkeypatch.setattr(web_mod, "keyless_search", AsyncMock(return_value=outcome_mod.SearchOutcome(WORKS)))
    assert await WebSearchTool().execute("q") == "No results for: q"
    key_store.set_value("web-search", VALUE)
    text = await WebSearchTool().execute("q", count=1)
    assert "1. Hit" in text and VALUE not in text
    assert seen[-1].headers["X-Subscription-Token"] == VALUE
    assert json.dumps(dict(seen[-1].url.params)).find(VALUE) == -1
