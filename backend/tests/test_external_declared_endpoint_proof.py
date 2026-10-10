"""A declared-model endpoint must prove itself before it is saved as reachable.

A mistyped base path answers the list call with 404, exactly like a provider
that keeps no model list, so saving or testing such an endpoint asks one
declared model for a reply and reads only its status (#2265).
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing

import pytest
from fastapi import HTTPException

from omlx_provider_fixtures import _patch_httpx, _resp, _Router

_BASE = "https://token-plan.example.com/compatible-mode/v1"
_LIST = ("GET", "/compatible-mode/v1/models")
_CHAT = ("POST", "/compatible-mode/v1/chat/completions")


def _quiet(monkeypatch) -> list:
    from infra import product_events
    from llm import network_discovery as nd

    events: list = []
    monkeypatch.setattr(nd, "discover_and_notify", lambda sid: asyncio.sleep(0))
    monkeypatch.setattr(
        product_events, "emit_product_event",
        lambda feature, action, **kw: events.append((action, (kw.get("properties") or {}).get("reason"))),
    )
    return events


def _add(base: str = _BASE, dialect: str = "openai", ids=("glm-5.3", "deepseek-v4.1-flash")) -> dict:
    from api.network_server_helpers import add_external_server
    from api.network_servers import AddServerBody

    body = AddServerBody(name="TP", base_url=base, dialect=dialect, api_key="k", model_ids=list(ids))
    return asyncio.run(add_external_server(body))


def _servers(path) -> int:
    with closing(sqlite3.connect(str(path))) as db:
        return db.execute("SELECT count(*) FROM network_servers WHERE provider='external'").fetchone()[0]


def test_a_mistyped_path_fails_at_save_and_nothing_is_stored(isolated_db, monkeypatch) -> None:
    events = _quiet(monkeypatch)
    _patch_httpx(monkeypatch, _Router({}))  # the list call and the model both 404
    with pytest.raises(HTTPException) as err:
        _add()
    assert "HTTP 404 for glm-5.3" in err.value.detail
    assert "base URL" in err.value.detail
    assert _servers(isolated_db) == 0
    assert events == [("register.failed", "declared_model_refused")]


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_key_fails_at_save(isolated_db, monkeypatch, status: int) -> None:
    _quiet(monkeypatch)
    _patch_httpx(monkeypatch, _Router({_CHAT: _resp(status, {"code": "InvalidApiKey"})}))
    with pytest.raises(HTTPException) as err:
        _add()
    assert f"refused the API key for glm-5.3 (HTTP {status})" in err.value.detail
    assert _servers(isolated_db) == 0


def test_the_proof_sends_only_the_model_and_one_message(isolated_db, monkeypatch) -> None:
    _quiet(monkeypatch)
    router = _Router({_CHAT: _resp(200)})
    _patch_httpx(monkeypatch, router)
    assert _add()["ok"] is True
    posts = [(url, body) for method, url, body in router.calls if method == "POST"]
    assert posts == [(f"{_BASE}/chat/completions", {
        "model": "glm-5.3", "stream": True,
        "messages": [{"role": "user", "content": "Reply with OK."}],
    })]


@pytest.mark.parametrize("status", [200, 400, 429, 500])
def test_any_other_answer_came_from_the_turn_path_and_saves(isolated_db, monkeypatch, status: int) -> None:
    _quiet(monkeypatch)
    _patch_httpx(monkeypatch, _Router({_CHAT: _resp(status)}))
    assert _add()["status"] == "online"


def test_a_listing_endpoint_is_never_asked_for_a_reply(isolated_db, monkeypatch) -> None:
    # The correct Token Plan base answers the list call (with no models): no proof needed.
    _quiet(monkeypatch)
    router = _Router({_LIST: _resp(200, {"data": []})})
    _patch_httpx(monkeypatch, router)
    assert _add()["ok"] is True
    assert [c for c in router.calls if c[0] == "POST"] == []


def test_an_anthropic_endpoint_is_asked_on_its_messages_path(isolated_db, monkeypatch) -> None:
    _quiet(monkeypatch)
    router = _Router({("POST", "/anthropic/v1/messages"): _resp(200)})
    _patch_httpx(monkeypatch, router)
    assert _add("https://gw.example.com/anthropic", "anthropic", ["glm-5.3"])["ok"] is True
    [(_m, url, body)] = [c for c in router.calls if c[0] == "POST"]
    assert url == "https://gw.example.com/anthropic/v1/messages"
    assert body["max_tokens"] == 1 and body["model"] == "glm-5.3"


def test_an_edit_to_a_mistyped_path_is_refused_and_the_old_url_kept(isolated_db, monkeypatch) -> None:
    from api.network_server_helpers import update_external_server
    from api.network_servers import UpdateExternalServerBody
    from llm import network_discovery as nd

    _quiet(monkeypatch)
    router = _Router({_CHAT: _resp(200)})
    _patch_httpx(monkeypatch, router)
    sid = _add()["server_id"]
    router.routes = {}
    with pytest.raises(HTTPException, match="HTTP 404 for glm-5.3"):
        asyncio.run(update_external_server(
            sid, UpdateExternalServerBody(base_url="https://token-plan.example.com/compatible-mode"),
        ))
    assert nd.get_server_metadata(sid)["base_url"] == _BASE


def test_test_connection_names_a_mistyped_path(monkeypatch) -> None:
    from api import external_connection_check as check
    from llm import network_discovery as nd

    _quiet(monkeypatch)
    monkeypatch.setattr(nd, "read_server_metadata",
                        lambda sid: {"base_url": _BASE, "declared_models": ["glm-5.3"]})
    _patch_httpx(monkeypatch, _Router({}))
    server = {"id": "ns-tp", "kind": "external", "dialect": "openai", "status": "online"}
    out = asyncio.run(check.check_external_server("ns-tp", server))
    assert out["ok"] is False
    assert "HTTP 404 for glm-5.3" in out["message"]
    assert f"{_BASE}/chat/completions" in out["message"]
