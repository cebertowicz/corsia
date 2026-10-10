
from __future__ import annotations

import pytest
from auth_fixtures import OWNER_TOKEN


@pytest.mark.integration
class TestHealthEndpoint:

    def test_health_returns_200(self, integration_client):
        resp = integration_client.get("/api/health")
        assert resp.status_code == 200

    def test_health_response_shape(self, integration_client):
        data = integration_client.get("/api/health").json()
        assert "status" in data
        assert data["status"] == "ok"

    def test_health_never_advertises_e2e_mock_mode(self, integration_client):
        assert "mock_mode" not in integration_client.get("/api/health").json()


_TOKEN = OWNER_TOKEN


@pytest.mark.integration
class TestSettingsEndpoint:

    def test_settings_returns_200(self, integration_client):
        resp = integration_client.get(f"/api/settings?token={_TOKEN}")
        assert resp.status_code == 200

    def test_settings_accepts_bearer_header(self, integration_client):
        resp = integration_client.get(
            "/api/settings", headers={"Authorization": f"Bearer {_TOKEN}"})
        assert resp.status_code == 200

    def test_settings_rejects_no_token(self, integration_client):
        assert integration_client.get("/api/settings").status_code == 401
        assert integration_client.get("/api/settings?token=wrong").status_code == 401

    def test_settings_response_shape(self, integration_client):
        data = integration_client.get(f"/api/settings?token={_TOKEN}").json()
        required_keys = [
            "workspace_context_files",
            "host_access_auto_approve",
            "features",
        ]
        for key in required_keys:
            assert key in data, f"Missing required key: {key}"

    def test_legacy_summary_settings_and_routes_are_retired(self, integration_client):
        data = integration_client.get(f"/api/settings?token={_TOKEN}").json()
        assert not any(key.startswith("summary_") for key in data)
        assert integration_client.get(f"/api/summaries?token={_TOKEN}").status_code == 404
        assert integration_client.get(f"/api/summaries/old?token={_TOKEN}").status_code == 404
        assert integration_client.get(f"/api/threads/old/timeline?token={_TOKEN}").status_code == 404

    def test_settings_features_is_dict(self, integration_client):
        data = integration_client.get(f"/api/settings?token={_TOKEN}").json()
        assert isinstance(data["features"], dict)



@pytest.mark.integration
class TestSystemAuthGap:

    def test_metrics_rejects_no_token(self, integration_client):
        assert integration_client.get("/api/system").status_code == 401
        assert integration_client.get(f"/api/system?token={_TOKEN}").status_code == 200

    def test_host_resources_rejects_no_token(self, integration_client):
        assert integration_client.get("/api/system/host-resources").status_code == 401

    def test_check_load_rejects_no_token(self, integration_client):
        r = integration_client.post(
            "/api/system/host-resources/check-load",
            json={"estimated_gb": 1, "provider": "lmstudio"})
        assert r.status_code == 401

    def test_health_stays_public(self, integration_client):
        assert integration_client.get("/api/health").status_code == 200
