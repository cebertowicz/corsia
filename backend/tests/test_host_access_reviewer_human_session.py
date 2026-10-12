from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth_fixtures import OWNER_SESSION_TOKEN, OWNER_TOKEN


def _client() -> TestClient:
    from api.system import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_reviewer_model_changes_require_a_human_session(isolated_db):
    from infra.settings import get_agent_defaults, set_setting

    set_setting("agents", {"defaults": {"host_access_reviewer_model": "codex/gpt-5-codex"}})
    with _client() as client:
        for body in (
            {"host_access_reviewer_model": "codex/gpt-5-codex"},
            {"host_access_reviewer_model": None},
        ):
            refused = client.patch(
                "/api/settings",
                headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
                json=body,
            )
            assert refused.status_code == 403
            assert refused.json()["detail"] == (
                "This change needs the signed-in person: open CORSIA and make it there."
            )

        set_model = client.patch(
            "/api/settings",
            headers={"Authorization": f"Bearer {OWNER_SESSION_TOKEN}"},
            json={"host_access_reviewer_model": "codex/gpt-5-codex"},
        )
        clear_model = client.patch(
            "/api/settings",
            headers={"Authorization": f"Bearer {OWNER_SESSION_TOKEN}"},
            json={"host_access_reviewer_model": None},
        )

    assert set_model.status_code == 200
    assert clear_model.status_code == 200
    assert "host_access_reviewer_model" not in get_agent_defaults()


def test_only_the_conductor_principal_may_set_the_reviewer_without_a_session(isolated_db):
    """The conductor exemption keys on the in-process principal, not on any stored kind."""
    from auth_fixtures import OWNER_USER, seed_account
    from infra.settings import get_agent_defaults

    service_bearer = "neutral-service-" + "reviewer-check"
    seed_account(OWNER_USER, service_bearer, kind="service")
    with _client() as client:
        for bearer in (OWNER_TOKEN, service_bearer):
            refused = client.patch(
                "/api/settings",
                headers={"Authorization": f"Bearer {bearer}"},
                json={"host_access_reviewer_model": "codex/gpt-5-codex"},
            )
            assert refused.status_code == 403

    assert "host_access_reviewer_model" not in get_agent_defaults()


def test_reviewer_fallback_changes_require_a_human_session(isolated_db):
    from infra.settings import get_agent_defaults

    with _client() as client:
        refused = client.patch(
            "/api/settings",
            headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
            json={"host_access_reviewer_fallback": True},
        )
        saved = client.patch(
            "/api/settings",
            headers={"Authorization": f"Bearer {OWNER_SESSION_TOKEN}"},
            json={"host_access_reviewer_fallback": True},
        )

    assert refused.status_code == 403
    assert refused.json()["detail"] == (
        "This change needs the signed-in person: open CORSIA and make it there."
    )
    assert saved.status_code == 200
    assert get_agent_defaults()["host_access_reviewer_fallback"] is True
