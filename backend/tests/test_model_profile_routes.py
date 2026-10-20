"""Harness model profile route coverage."""

from __future__ import annotations

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def client(isolated_db, monkeypatch):
    from api import models as api_models
    from infra.database import get_db

    # Exercise real transaction/profile resolution, including full slash IDs.
    db = get_db()
    try:
        for harness, provider, name, model_id in (
            ("claude-code", "claude-code", "opus[1m]", "claude-code/opus[1m]"),
            ("codex", "codex", "gpt-5.6-luna", "codex/gpt-5.6-luna"),
            ("native", "omlx", "Qwen3.8-Flash-Next", "omlx@srv-test/Qwen3.8-Flash-Next"),
        ):
            db.execute(
                "INSERT INTO models (id,harness,provider,provider_model_id,"
                "litellm_model,display_name,source,context_window,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?, 'test',1000000,'t0','t0')",
                (model_id, harness, provider, name, model_id, name),
            )
        db.commit()
    finally:
        db.close()

    from infra.auth import require_user

    app = FastAPI()
    app.dependency_overrides[require_user] = lambda: {"id": "test"}
    app.include_router(api_models.router)
    return TestClient(app)


@pytest.mark.parametrize(
    "model_id",
    ("claude-code/opus[1m]", "codex/gpt-5.6-luna", "omlx@srv-test/Qwen3.8-Flash-Next"),
)
def test_model_profile_routes_capture_full_model_id_and_store_context_cap(
    client, model_id
):
    initial = client.get(f"/api/models/{model_id}/profile")
    updated = client.patch(
        f"/api/models/{model_id}/profile?token=test-token",
        json={"context_window": 262_144},
    )
    saved = client.get(f"/api/models/{model_id}/profile")

    assert initial.status_code == 200
    assert initial.json()["model_id"] == model_id
    assert updated.status_code == 200
    assert updated.json() == {
        "ok": True,
        "model_id": model_id,
        "updated": ["context_window"],
    }
    assert saved.status_code == 200
    assert saved.json()["context_window"] == 262_144
    # Discovery fills context_window; only a saved value is reported as the
    # override. Nothing is saved at first, so an ordinary row reports the CORSIA
    # default and only a selector that names the bigger window by name keeps the
    # provider's own value.
    extended = "[1m]" in model_id
    assert initial.json()["context_window"] == (1_000_000 if extended else 262_144)
    assert initial.json()["context_window_source"] == ("provider" if extended else "default")
    assert initial.json()["context_window_override"] is None
    assert saved.json()["context_window_override"] == 262_144
    assert saved.json()["context_window_source"] == "saved"
    # Clearing the saved window lands on what applied before it was saved.
    for profile in (initial.json(), saved.json()):
        assert (profile["context_window_unsaved"], profile["context_window_unsaved_source"]) == (
            initial.json()["context_window"], initial.json()["context_window_source"],
        )


@pytest.mark.parametrize("invalid_window", (-1, 0, 1.5, True, "262144"))
def test_profile_route_rejects_invalid_context_window(client, invalid_window):
    response = client.patch(
        "/api/models/codex/gpt-5.6-luna/profile",
        params={"token": __name__},
        json={"context_window": invalid_window},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "context_window must be a positive integer"


def test_profile_route_allows_clearing_context_window(client):
    response = client.patch(
        "/api/models/codex/gpt-5.6-luna/profile",
        params={"token": __name__},
        json={"context_window": None},
    )

    assert response.status_code == 200


def test_profile_route_reports_the_resolved_window_not_the_raw_saved_one(client):
    """A harness cap above the discovered window cannot raise what is reported."""
    model_id = "codex/gpt-5.6-luna"
    client.patch(
        f"/api/models/{model_id}/profile",
        params={"token": "t"},
        json={"context_window": 2_000_000},
    )
    saved = client.get(f"/api/models/{model_id}/profile").json()

    assert saved["context_window"] == 1_000_000
    assert saved["context_window_override"] == 2_000_000


def test_profile_route_ignores_output_cap(client, isolated_db):
    """Output limits are the provider's: PATCH does not store max_tokens."""
    from infra.database import get_db

    model_id = "codex/gpt-5.6-luna"
    response = client.patch(
        f"/api/models/{model_id}/profile",
        params={"token": "t"},
        json={"max_tokens": 64000, "context_window": 262_144},
    )
    db = get_db()
    stored = db.execute(
        "SELECT max_tokens FROM model_profiles WHERE model_id = ?", (model_id,)
    ).fetchone()
    db.close()

    assert response.json()["updated"] == ["context_window"]
    assert stored["max_tokens"] is None


def test_invalid_saved_window_reads_as_no_override(client, isolated_db):
    """A hand-edited invalid value is shown the same way dispatch treats it."""
    from infra.database import get_db

    model_id = "ext-invalid"
    db = get_db()
    db.execute(
        "INSERT INTO models (id,harness,provider,provider_model_id,litellm_model,"
        "display_name,context_window,created_at,updated_at) VALUES "
        "(?,'native','external','m','external@ns-x/m','m',131072,'t0','t0')",
        (model_id,),
    )
    db.execute(
        "INSERT INTO model_profiles (model_id, context_window) VALUES (?, ?)",
        (model_id, "not-a-number"),
    )
    db.commit()
    db.close()

    saved = client.get(f"/api/models/{model_id}/profile").json()

    assert saved["context_window_override"] is None
    assert saved["context_window"] == 131072
