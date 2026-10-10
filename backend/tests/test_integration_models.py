
from __future__ import annotations

import pytest


@pytest.mark.integration
class TestModelsEndpoint:

    def test_models_returns_200(self, integration_client):
        resp = integration_client.get("/api/models")
        assert resp.status_code == 200

    def test_models_response_shape(self, integration_client):
        data = integration_client.get("/api/models").json()
        assert isinstance(data, dict)
        assert "models" in data
        assert isinstance(data["models"], list)

    def test_models_active_local_threads_field(self, integration_client):
        data = integration_client.get("/api/models").json()
        assert "active_local_threads" in data
        assert isinstance(data["active_local_threads"], int)

    def test_models_entry_shape(self, integration_client):
        data = integration_client.get("/api/models").json()
        for entry in data["models"]:
            assert "id" in entry, f"Model entry missing 'id': {entry}"
            assert "name" in entry, f"Model entry missing 'name': {entry}"
