"""Usage sharing: on until the person turns it off, saved on the settings PATCH."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from auth_fixtures import OWNER_SESSION_TOKEN

pytestmark = pytest.mark.usefixtures('signed_in_defaults')
HEADERS = {"Authorization": f"Bearer {OWNER_SESSION_TOKEN}"}


def _client() -> TestClient:
    from api.system import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_sharing_is_on_until_the_person_turns_it_off(isolated_db):
    with _client() as client:
        initial = client.get("/api/settings", headers=HEADERS)
        off = client.patch("/api/settings", headers=HEADERS, json={"usage_sharing": False})
        after_off = client.get("/api/settings", headers=HEADERS)
        on = client.patch("/api/settings", headers=HEADERS, json={"usage_sharing": True})
        after_on = client.get("/api/settings", headers=HEADERS)

    assert initial.json()["usage_sharing"] is True
    assert off.json()["usage_sharing"] is False
    assert after_off.json()["usage_sharing"] is False
    assert on.json()["usage_sharing"] is True
    assert after_on.json()["usage_sharing"] is True


def test_a_non_boolean_is_refused_and_nothing_is_saved(isolated_db):
    from infra.settings import USAGE_SHARING_KEY, get_setting

    with _client() as client:
        invalid = client.patch("/api/settings", headers=HEADERS, json={"usage_sharing": "no"})

    assert invalid.status_code == 400
    assert get_setting(USAGE_SHARING_KEY) is None


@pytest.mark.parametrize("raw", ['{"enabled": "off"}', "not json", "[false]"])
def test_a_damaged_saved_value_reads_as_off(isolated_db, raw):
    from infra.database import get_db
    from infra.settings import USAGE_SHARING_KEY, get_usage_sharing

    db = get_db()
    db.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, '2026-11-03T00:00:00Z')",
               (USAGE_SHARING_KEY, raw))
    db.commit()

    assert get_usage_sharing() is False
