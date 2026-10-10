from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth_fixtures import OWNER_TOKEN


def _client() -> TestClient:
    from api.system import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_security_alert_requires_an_authenticated_user(isolated_db):
    with _client() as client:
        anonymous = client.get("/api/security/alert")
        authenticated = client.get(
            "/api/security/alert",
            headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
        )

    assert anonymous.status_code == 401
    assert authenticated.status_code == 200
