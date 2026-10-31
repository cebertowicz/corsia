from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from auth_fixtures import OWNER_SESSION_TOKEN, OWNER_TOKEN

pytestmark = pytest.mark.usefixtures('signed_in_defaults')


def _client() -> TestClient:
    from api.system import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _set_defaults(**updates) -> None:
    from infra.settings import set_setting

    set_setting("agents", {"defaults": updates})


def _get_settings(client: TestClient):
    return client.get(
        "/api/settings",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
    )


def test_settings_reports_reviewer_unconfigured(isolated_db):
    _set_defaults()

    with _client() as client:
        response = _get_settings(client)

    assert response.status_code == 200
    assert response.json()["host_access_reviewer_model"] is None
    assert response.json()["host_access_reviewer_model_name"] is None
    assert response.json()["host_access_reviewer_status"] == "requester_model"
    assert response.json()["host_access_reviewer_message"] == "Using the requesting agent's model."


def test_settings_reports_exact_ready_and_missing_reviewer(isolated_db):
    _set_defaults(host_access_reviewer_model="claude-code/opus")

    with _client() as client:
        ready = _get_settings(client)

    assert ready.status_code == 200
    assert ready.json()["host_access_reviewer_model"] == "claude-code/opus"
    assert ready.json()["host_access_reviewer_status"] == "ready"
    assert "opus" in ready.json()["host_access_reviewer_model_name"].lower()
    assert ready.json()["host_access_reviewer_message"] is None

    _set_defaults(host_access_reviewer_model="claude-code/removed")
    with _client() as client:
        missing = _get_settings(client)

    assert missing.status_code == 200
    assert missing.json()["host_access_reviewer_model"] == "claude-code/removed"
    assert missing.json()["host_access_reviewer_model_name"] == "claude-code/removed"
    assert missing.json()["host_access_reviewer_status"] == "unavailable"
    assert missing.json()["host_access_reviewer_message"] == (
        "claude-code/removed can't review requests right now (not in the catalog). "
        "Host-access requests will fail. Choose another model or turn on fallback."
    )


def test_settings_patch_accepts_only_canonical_ready_public_model(isolated_db):
    from infra.settings import get_agent_defaults

    _set_defaults()
    with _client() as client:
        accepted = client.patch(
            "/api/settings",
            headers={"Authorization": f"Bearer {OWNER_SESSION_TOKEN}"},
            json={"host_access_reviewer_model": "codex/gpt-5-codex"},
        )
        noncanonical = client.patch(
            "/api/settings",
            headers={"Authorization": f"Bearer {OWNER_SESSION_TOKEN}"},
            json={"host_access_reviewer_model": "gpt-5-codex"},
        )

    assert accepted.status_code == 200
    assert accepted.json()["host_access_reviewer_model"] == "codex/gpt-5-codex"
    assert get_agent_defaults()["host_access_reviewer_model"] == (
        "codex/gpt-5-codex"
    )
    assert noncanonical.status_code == 400


def test_settings_patch_clears_reviewer_model(isolated_db):
    from infra.settings import get_agent_defaults

    _set_defaults(host_access_reviewer_model="codex/gpt-5-codex")
    with _client() as client:
        cleared = client.patch(
            "/api/settings",
            headers={"Authorization": f"Bearer {OWNER_SESSION_TOKEN}"},
            json={"host_access_reviewer_model": None},
        )

    assert cleared.status_code == 200
    assert cleared.json()["host_access_reviewer_model"] is None
    assert "host_access_reviewer_model" not in get_agent_defaults()


def test_settings_persists_fail_closed_host_access_auto_approval(isolated_db):
    from infra.settings import get_agent_defaults

    _set_defaults()
    with _client() as client:
        initial = _get_settings(client)
        denied = client.patch(
            "/api/settings",
            headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
            json={"host_access_auto_approve": True},
        )
        enabled = client.patch(
            "/api/settings",
            headers={"Authorization": f"Bearer {OWNER_SESSION_TOKEN}"},
            json={"host_access_auto_approve": True},
        )
        invalid = client.patch(
            "/api/settings",
            headers={"Authorization": f"Bearer {OWNER_SESSION_TOKEN}"},
            json={"host_access_auto_approve": "yes"},
        )

    assert initial.json()["host_access_auto_approve"] is False
    assert denied.status_code == 403
    assert denied.json()["detail"] == (
        "This change needs the signed-in person: open CORSIA and make it there."
    )
    assert enabled.status_code == 200
    assert enabled.json()["host_access_auto_approve"] is True
    assert get_agent_defaults()["host_access_auto_approve"] is True
    assert invalid.status_code == 400


def test_api_token_can_still_update_non_sensitive_settings(isolated_db):
    with _client() as client:
        response = client.patch(
            "/api/settings",
            headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
            json={"features": {"focus_mode": False}},
        )

    assert response.status_code == 200
    assert response.json()["features"]["focus_mode"] is False


def test_harness_cleanup_retains_configured_reviewer_reference(isolated_db):
    from connections.harness_legacy_fallback import remove_legacy_fallback
    from infra.database import get_db

    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO models "
        "(id,harness,provider,provider_model_id,litellm_model,display_name,"
        "backend,source,enabled,available,metadata_json,created_at,updated_at) "
        "VALUES ('claude-code/legacy','claude-code','claude-code','legacy',"
        "'claude-code/legacy','Legacy','cc','legacy_fallback',1,0,'{}','t','t')"
    )
    db.execute(
        "INSERT OR REPLACE INTO settings (key,value,updated_at) VALUES (?,?,?)",
        (
            "agents",
            json.dumps(
                {
                    "defaults": {
                        "host_access_reviewer_model": "claude-code/legacy"
                    }
                }
            ),
            "t",
        ),
    )
    db.commit()

    remove_legacy_fallback("claude-code", db=db)

    retained = db.execute(
        "SELECT source,enabled,available FROM models "
        "WHERE id='claude-code/legacy'"
    ).fetchone()
    assert retained is not None
    assert tuple(retained) == ("reference", 0, 0)
    db.close()


def _owner_patch(client: TestClient, body: dict):
    return client.patch(
        "/api/settings",
        headers={"Authorization": f"Bearer {OWNER_SESSION_TOKEN}"},
        json=body,
    )


def test_reviewer_fallback_defaults_off_and_saves_a_boolean(isolated_db):
    from infra.settings import get_agent_defaults

    _set_defaults()
    with _client() as client:
        initial = _get_settings(client)
        enabled = _owner_patch(client, {"host_access_reviewer_fallback": True})
        invalid = _owner_patch(client, {"host_access_reviewer_fallback": "yes"})
        after_invalid = _get_settings(client)
        disabled = _owner_patch(client, {"host_access_reviewer_fallback": False})

    assert initial.json()["host_access_reviewer_fallback"] is False
    assert enabled.status_code == 200
    assert enabled.json()["host_access_reviewer_fallback"] is True
    assert invalid.status_code == 400
    assert after_invalid.json()["host_access_reviewer_fallback"] is True
    assert disabled.json()["host_access_reviewer_fallback"] is False
    assert get_agent_defaults()["host_access_reviewer_fallback"] is False


def test_reviewer_fallback_survives_a_reviewer_model_change(isolated_db):
    _set_defaults(host_access_reviewer_fallback=True)
    with _client() as client:
        chosen = _owner_patch(client, {"host_access_reviewer_model": "codex/gpt-5-codex"})
        after_choice = _get_settings(client)
        cleared = _owner_patch(client, {"host_access_reviewer_model": None})
        after_clear = _get_settings(client)

    assert chosen.status_code == 200
    assert cleared.status_code == 200
    assert after_choice.json()["host_access_reviewer_fallback"] is True
    assert after_clear.json()["host_access_reviewer_fallback"] is True
