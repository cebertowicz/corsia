"""Provider and catalog settings routes authenticate by Bearer header as well as ?token=.

Agents must send their API token in the Authorization header, never a URL, so every
provider/catalog route has to accept it (#2211). The query form stays for the UI.
"""

from __future__ import annotations

import os
import sys

import pytest

from auth_fixtures import seed_account
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TOKEN = "test-header-auth-01"

# One route per family. An authenticated call to an unknown model answers 404, so
# 401 versus not-401 isolates the auth check from each handler's own work.
_ROUTES = [
    ("GET", "/api/settings/providers", None),
    ("GET", "/api/settings/providers/anthropic/status", None),
    ("GET", "/api/settings/models", None),
    ("PATCH", "/api/settings/models/no-such-model", {"alias": "x"}),
    ("PATCH", "/api/models/no-such-model/profile", {"context_window": 131072}),
]


@pytest.fixture
def client(monkeypatch):
    seed_account("u-header-auth", _TOKEN)
    import connections.harness_refresh as harness_refresh
    from api import discovered_models, models, providers

    # GET /api/models schedules background harness CLI checks; the real ones can
    # outlive the request and hang the TestClient's loop shutdown.
    monkeypatch.setattr(harness_refresh, "schedule_stale_refresh", lambda: None)

    app = FastAPI()
    for module in (providers, discovered_models, models):
        app.include_router(module.router)
    return TestClient(app)


@pytest.mark.parametrize(("method", "path", "body"), _ROUTES)
def test_bearer_header_is_accepted(client, method, path, body):
    response = client.request(method, path, json=body, headers={"Authorization": f"Bearer {_TOKEN}"})
    assert response.status_code != 401, response.text


@pytest.mark.parametrize(("method", "path", "body"), _ROUTES)
def test_query_token_still_works_for_the_ui(client, method, path, body):
    response = client.request(method, f"{path}?token={_TOKEN}", json=body)
    assert response.status_code != 401, response.text


@pytest.mark.parametrize(("method", "path", "body"), _ROUTES)
def test_missing_or_wrong_credential_is_refused(client, method, path, body):
    assert client.request(method, path, json=body).status_code == 401
    wrong = client.request(method, path, json=body, headers={"Authorization": "Bearer not-a-token"})
    assert wrong.status_code == 401


def test_public_catalog_reads_stay_public(client):
    assert client.get("/api/models").status_code == 200
