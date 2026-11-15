"""Declared-model setup proves reachability without running a model.

A 404/405 list response cannot distinguish a missing catalog from a wrong
base path. Preserve declared IDs without inference; actual use tests serving.
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

    body = AddServerBody(name="TP", base_url=base, dialect=dialect, api_key="[REDACTED]", model_ids=list(ids))
    return asyncio.run(add_external_server(body))


def _servers(path) -> int:
    with closing(sqlite3.connect(str(path))) as db:
        return db.execute("SELECT count(*) FROM network_servers WHERE provider='external'").fetchone()[0]


@pytest.mark.parametrize("status", [401, 403, 500])
def test_a_refused_list_fails_at_save_without_inference(isolated_db, monkeypatch, status: int) -> None:
    events = _quiet(monkeypatch)
    wire = _Router({_LIST: _resp(status)})
    _patch_httpx(monkeypatch, wire)
    with pytest.raises(HTTPException) as err:
        _add()
    assert f"HTTP {status}" in err.value.detail
    assert _servers(isolated_db) == 0
    assert events == [("register.failed", f"http_{status}")]
    assert wire.calls and all(method == "GET" for method, _url, _body in wire.calls)


def test_a_listing_endpoint_is_never_asked_for_a_reply(isolated_db, monkeypatch) -> None:
    _quiet(monkeypatch)
    wire = _Router({_LIST: _resp(200, {"data": []})})
    _patch_httpx(monkeypatch, wire)
    assert _add()["ok"] is True
    assert wire.calls and all(method == "GET" for method, _url, _body in wire.calls)


def test_an_edit_without_a_list_preserves_ids_without_inference(isolated_db, monkeypatch) -> None:
    from api.network_server_helpers import update_external_server
    from api.network_servers import UpdateExternalServerBody
    from llm import network_discovery as nd

    _quiet(monkeypatch)
    wire = _Router({})  # Any model-list path returns 404.
    _patch_httpx(monkeypatch, wire)
    sid = _add()["server_id"]
    changed_base = "https://token-plan.example.com/compatible-mode"
    result = asyncio.run(update_external_server(
        sid, UpdateExternalServerBody(base_url=changed_base),
    ))
    assert result["ok"] is True
    stored = nd.get_server_metadata(sid)
    assert stored["base_url"] == changed_base
    assert stored["declared_models"] == ["glm-5.3", "deepseek-v4.1-flash"]
    assert wire.calls and all(method == "GET" for method, _url, _body in wire.calls)


def test_test_connection_names_a_refused_list_without_inference(monkeypatch) -> None:
    from api import external_connection_check as check
    from llm import network_discovery as nd

    _quiet(monkeypatch)
    monkeypatch.setattr(nd, "read_server_metadata",
                        lambda sid: {"base_url": _BASE, "declared_models": ["glm-5.3"]})
    wire = _Router({_LIST: _resp(401)})
    _patch_httpx(monkeypatch, wire)
    server = {"id": "ns-tp", "kind": "external", "dialect": "openai", "status": "online"}
    out = asyncio.run(check.check_external_server("ns-tp", server))
    assert out["ok"] is False
    assert "HTTP 401" in out["message"]
    assert f"{_BASE}/models" in out["message"]
    assert wire.calls and all(method == "GET" for method, _url, _body in wire.calls)
