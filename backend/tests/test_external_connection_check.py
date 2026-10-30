"""Test connection on an External endpoint answers from the endpoint, now."""

from __future__ import annotations

import asyncio

import pytest

from omlx_provider_fixtures import _patch_httpx, _resp, _Router
from tests.test_provider_sources import client  # noqa: F401

_BASE = "https://token-plan.example.com/compatible-mode/v1"
_LIST = "/compatible-mode/v1/models"
_SERVER = {"id": "ns-tp", "kind": "external", "dialect": "openai", "port": 443,
           "status": "online", "provider": "external"}
# The image re-check steps the last Test connection took, in order.
_PROBES: list[tuple] = []


def _check(monkeypatch, reply, *, meta=None, server=None, discovery=None):
    from api import external_connection_check as check
    from infra import product_events
    from llm import network_discovery as nd

    stored = {"base_url": _BASE, "declared_models": ["glm-5.3"]} if meta is None else meta
    after = {**(stored or {}), "discovery": discovery or {"models": 1, "error": None}}
    reads = iter([stored, after])
    swept: list = []
    _PROBES.clear()

    async def detect(sid):
        _PROBES.append(("probe", sid))
        return 0

    async def sweep(_client, srv):
        swept.append(srv["id"])

    async def rediscover(_sid):
        summary = after["discovery"]
        return {"ok": not summary.get("error"), "error": summary.get("error"),
                "discovered": summary.get("models", 0), "updated": 0}

    monkeypatch.setattr(nd, "read_server_metadata", lambda sid: next(reads))
    monkeypatch.setattr(nd, "_sweep_one", sweep)
    monkeypatch.setattr(nd, "_discover_and_notify", rediscover)
    monkeypatch.setattr(check, "detect_image_input", detect)
    monkeypatch.setattr(check, "mark_for_recheck", lambda sid: _PROBES.append(("mark", sid)))
    monkeypatch.setattr(check, "image_input_summary", lambda sid: "Images: glm-5.3.")
    monkeypatch.setattr(product_events, "emit_product_event", lambda *a, **k: None)
    # A declared model answers, so a missing list call is the provider's, not a bad path.
    chat = ("POST", "/compatible-mode/v1/chat/completions")
    _patch_httpx(monkeypatch, _Router({("GET", _LIST): reply, chat: _resp(200)}))
    return asyncio.run(check.check_external_server("ns-tp", server or _SERVER)), swept


def test_a_reachable_endpoint_is_swept_rediscovered_and_counted(monkeypatch) -> None:
    out, swept = _check(monkeypatch, _resp(200, {"data": []}), discovery={"models": 2, "error": None})
    assert out == {"ok": True, "message": "Reachable. 2 models found. Images: glm-5.3."}
    assert swept == ["ns-tp"]
    # The test marks every detected model for a re-check before rediscovering.
    assert _PROBES == [("mark", "ns-tp"), ("probe", "ns-tp")]


def test_a_missing_list_call_passes_when_ids_are_declared(monkeypatch) -> None:
    out, _ = _check(monkeypatch, _resp(404))
    assert out["ok"] is True


@pytest.mark.parametrize("reply,named", [
    (_resp(401, {"code": "InvalidApiKey"}), "HTTP 401"),
    (_resp(500), "HTTP 500"),
])
def test_a_refusal_is_named_and_nothing_is_recorded(monkeypatch, reply, named: str) -> None:
    out, swept = _check(monkeypatch, reply)
    assert out["ok"] is False and named in out["message"]
    assert swept == []


def test_a_discovery_failure_after_reaching_it_is_reported(monkeypatch) -> None:
    out, _ = _check(monkeypatch, _resp(200, {"data": []}), discovery={"models": 0, "error": "boom"})
    assert out == {"ok": False, "message": "Reached, but model discovery failed: boom"}


@pytest.mark.parametrize("aggregate", [False, True])
def test_a_failed_discovery_receipt_is_not_hidden_by_old_saved_metadata(
    isolated_db, client, monkeypatch, aggregate,
):
    from api import external_connection_check as check
    from llm import external_discovery_result, external_provider, network_discovery as nd

    async def probe(*_a, **_kw):
        return None

    async def sweep(*_a):
        return None

    async def models(*_a):
        raise OSError("transport failed")

    async def observe(*_a):
        return None

    def cannot_save(*_a, **_kw):
        raise OSError("database is locked")

    # Exercise the actual discovery -> failed save -> Test receipt boundary.
    monkeypatch.setattr(check, "_probe_external", probe)
    monkeypatch.setattr(check, "mark_for_recheck", lambda _sid: None)
    monkeypatch.setattr(nd, "_sweep_one", sweep)
    monkeypatch.setattr(nd, "get_server", lambda _sid: _SERVER)
    monkeypatch.setattr(nd, "read_server_metadata", lambda _sid: {
        "base_url": _BASE, "discovery": {"models": 9, "error": None}})
    monkeypatch.setattr(external_provider, "_external_discovery_config",
                        lambda _sid: (_SERVER, _BASE, None, {}))
    monkeypatch.setattr("llm.external_declared_models.discoverable_models", models)
    monkeypatch.setattr("llm.server_observation.observe_discovery", observe)
    monkeypatch.setattr(external_discovery_result, "record_external_discovery", cannot_save)
    if aggregate:
        monkeypatch.setattr(nd, "get_servers", lambda: [_SERVER])
        response = client.post("/api/providers/sources/recheck")
        assert response.status_code == 503
        assert "sources" not in response.json()
        assert "transport failed" not in response.text
    else:
        out = asyncio.run(check.check_external_server("ns-tp", _SERVER))
        assert out["ok"] is False
        assert "transport failed" in out["message"]


def test_a_switched_off_endpoint_is_not_probed(monkeypatch) -> None:
    out, swept = _check(monkeypatch, _resp(200, {"data": []}), server={**_SERVER, "status": "disabled"})
    assert out["ok"] is False and "switched off" in out["message"]
    assert swept == []
