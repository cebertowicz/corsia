"""Settings Start persists status before real catalog discovery."""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth_fixtures import seed_account
from catalog_omlx_fixtures import _Router, _patch_httpx, _resp
from llm import network_discovery as nd


@pytest.mark.parametrize("case", ["empty", "model", "failure", "stop_failure"])
def test_stopped_managed_start_uses_real_discovery_and_truthful_status(monkeypatch, case):
    from api.network_servers import router
    from llm.catalog import list_models

    test_credential = "test-local-start-boundary"
    seed_account("local-start-boundary", test_credential)
    sid = nd.upsert_server("omlx", "corsia-managed", "CORSIA", "127.0.0.1", 18210, "offline", source="corsia")
    assert nd.get_server(sid)["status"] == "offline"
    monkeypatch.setattr("llm.local_setup_gates.host_ram_bytes", lambda: 24 * 1024**3)
    status = 503 if case == "failure" else 200
    models = [{"id": "test-model", "loaded": False, "max_context_window": 32768}] if case == "model" else []
    http = _Router({
        ("GET", "/v1/models/status"): _resp(status, {"models": models}),
        ("GET", "/admin/api/status"): _resp(200, {}),
        ("GET", "/v1/models"): _resp(200, {"data": []}),
    })
    _patch_httpx(monkeypatch, http)
    async def start_process(_cfg):
        # A successful supervisor health check marks the endpoint reachable.
        # Deliberately leave the stored server offline to exercise lifecycle ordering.
        from llm.network_endpoints import upsert_endpoint
        upsert_endpoint(sid, "127.0.0.1", ok=True, latency_ms=0)
        return {"ok": True}

    start = AsyncMock(side_effect=start_process)
    stop = AsyncMock(return_value={"ok": False, "message": "stop failed"} if case == "stop_failure" else {"ok": True})
    app = FastAPI()
    app.include_router(router)
    with patch("llm.providers.omlx.start_server", start), patch("llm.providers.omlx.stop_server", stop):
        response = TestClient(app).post(
            f"/api/settings/network-servers/{sid}/lifecycle/start",
            headers={"Authorization": f"Bearer {test_credential}"},
        )
    assert response.status_code == 200
    assert any(method == "GET" and "/v1/models/status" in url for method, url in http.calls)
    result = response.json()
    if case == "empty":
        assert result["reason"] == "mac_cannot_run"
        stop.assert_awaited_once_with({"server_id": sid})
        assert nd.get_server(sid)["status"] == "offline"
        assert not [r for r in list_models(provider="omlx", include_unavailable=False)
                    if r["litellm_model"].startswith(f"omlx@{sid}/")]
    elif case == "model":
        assert result["ok"] is True
        stop.assert_not_awaited()
        assert nd.get_server(sid)["status"] == "online"
        assert any(r["litellm_model"] == f"omlx@{sid}/test-model"
                   for r in list_models(provider="omlx", include_unavailable=False))
    elif case == "failure":
        assert result["ok"] is False and result["ref"].startswith("local-")
        stop.assert_not_awaited()
        assert nd.get_server(sid)["status"] == "online"
    else:
        assert result["ok"] is False and result["message"] == "stop failed"
        stop.assert_awaited_once_with({"server_id": sid})
        assert nd.get_server(sid)["status"] == "online"
