from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from auth_fixtures import OWNER_TOKEN

from catalog_omlx_fixtures import (
    _Router,
    _patch_httpx,
    _resp,
    _set_router,
    _stub_network_helpers,
    fresh_db,
)


def test_canonical_id_load_resolution(monkeypatch, fresh_db):
    _stub_network_helpers(monkeypatch)
    router = _Router({
        ("GET", "/v1/models/status"): _resp(200, {"models": [
            {
                "id": "Model-ABC-MLX-8bit",
                "loaded": False,
                "engine_type": "llm",
                "max_context_window": 128000,
            },
        ]}),
        ("GET", "/admin/api/status"): _resp(200, {}),
    })
    _patch_httpx(monkeypatch, router)

    from llm.catalog import discover_network_models
    asyncio.run(discover_network_models("srv-test"))

    conn = sqlite3.connect(str(fresh_db))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT provider_model_id FROM models WHERE provider = 'omlx'"
    ).fetchone()
    conn.close()
    assert row["provider_model_id"] == "Model-ABC-MLX-8bit"


def test_rediscovery_is_idempotent(monkeypatch, fresh_db):
    _stub_network_helpers(monkeypatch)
    router = _Router({
        ("GET", "/v1/models/status"): _resp(200, {"models": [
            {
                "id": "Model-XYZ-MLX-8bit",
                "loaded": True,
                "engine_type": "vlm",
                "max_context_window": 256000,
            },
        ]}),
        ("GET", "/admin/api/status"): _resp(200, {"device": "mlx"}),
    })
    _patch_httpx(monkeypatch, router)

    from llm.catalog import discover_network_models
    r1 = asyncio.run(discover_network_models("srv-test"))
    r2 = asyncio.run(discover_network_models("srv-test"))

    assert r1["discovered"] == 1
    assert r1["updated"] == 0
    assert r2["discovered"] == 0
    assert r2["updated"] == 1

    conn = sqlite3.connect(str(fresh_db))
    rows = conn.execute(
        "SELECT COUNT(*), MAX(supports_vision) FROM models WHERE provider = 'omlx'"
    ).fetchone()
    conn.close()
    assert rows[0] == 1
    assert rows[1] == 1


def test_discovery_enriches_context_window_from_generation_config(monkeypatch, fresh_db):
    _stub_network_helpers(monkeypatch)
    from llm import network_discovery as nd
    monkeypatch.setattr(
        nd,
        "get_server",
        lambda sid: {
            "id": sid, "provider": "omlx",
            "instance_id": "inst-1", "instance_name": "Mac",
            "host": "127.0.0.1", "port": 5100, "status": "online",
        } if sid == "srv-test" else None,
    )

    provider_model_id = "synthetic/Local-Model-A3B-8bit"
    router = _Router({
        ("GET", "/v1/models/status"): _resp(200, {"models": [
            {
                "id": provider_model_id,
                "loaded": True,
                "engine_type": "llm",
                "estimated_size": 37_000_000_000,
            },
        ]}),
        ("GET", "/admin/api/status"): _resp(200, {"device": "mlx"}),
        (
            "GET",
            f"/admin/api/models/{provider_model_id}/generation_config",
        ): _resp(200, {
            "config": {"max_position_embeddings": 262144},
            "generation_defaults": {},
        }),
        ("GET", "/admin/api/models"): _resp(200, {"data": [
            {"id": provider_model_id, "settings": {}},
        ]}),
    })
    _patch_httpx(monkeypatch, router)

    from llm.catalog import discover_network_models
    result = asyncio.run(discover_network_models("srv-test"))

    assert result["ok"], result
    conn = sqlite3.connect(str(fresh_db))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT provider_model_id, context_window FROM models WHERE provider = 'omlx'"
    ).fetchone()
    conn.close()

    assert row["provider_model_id"] == provider_model_id
    assert row["context_window"] == 262144


@pytest.mark.parametrize(
    ("reported_default", "expected_present", "expected_value"),
    [
        (True, True, True),
        (False, True, False),
        ("true", False, None),
        (None, False, None),
    ],
)
def test_discovery_stores_only_boolean_preserve_thinking_default(
    monkeypatch,
    fresh_db,
    reported_default,
    expected_present,
    expected_value,
):
    _stub_network_helpers(monkeypatch)
    status_model = {
        "id": "ReasoningModel",
        "loaded": True,
        "engine_type": "llm",
        "max_context_window": 128000,
    }
    if reported_default is not None:
        status_model["preserve_thinking_default"] = reported_default
    router = _Router({
        ("GET", "/v1/models/status"): _resp(200, {"models": [status_model]}),
        ("GET", "/admin/api/status"): _resp(200, {}),
    })
    _patch_httpx(monkeypatch, router)

    from llm.catalog import discover_network_models
    result = asyncio.run(discover_network_models("srv-test"))

    assert result["ok"], result
    conn = sqlite3.connect(str(fresh_db))
    raw = conn.execute(
        "SELECT metadata_json FROM models WHERE provider = 'omlx'"
    ).fetchone()[0]
    conn.close()
    metadata = json.loads(raw)
    assert ("preserve_thinking_default" in metadata) is expected_present
    if expected_present:
        assert metadata["preserve_thinking_default"] is expected_value


def test_api_list_models_returns_canonical_id_only(monkeypatch, fresh_db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from llm import network_discovery as nd
    from llm import network_endpoints as ne
    monkeypatch.setattr(
        nd, "get_server",
        lambda sid: {
            "id": sid, "provider": "omlx",
            "instance_id": "inst-1", "instance_name": "Mac",
            "host": "127.0.0.1", "port": 5100, "status": "online",
        } if sid == "srv-test" else None,
    )
    monkeypatch.setattr(ne, "build_api_base", lambda sid: "http://127.0.0.1:5100/v1")
    monkeypatch.setattr(ne, "build_mgmt_base", lambda sid: "http://127.0.0.1:5100/admin/api")

    router = _Router({
        ("GET", "/v1/models/status"): _resp(200, {"models": [
            {
                "id": "Model-ABC-MLX-8bit",
                "loaded": True,
                "engine_type": "llm",
                "max_context_window": 128000,
                "estimated_size": 200_000_000_000,
            },
            {
                "id": "Model-XYZ-MLX-8bit",
                "loaded": False,
                "engine_type": "vlm",
                "max_context_window": 256000,
                "estimated_size": 36_000_000_000,
            },
        ]}),
        ("GET", "/admin/api/status"): _resp(200, {"device": "mlx"}),
        ("GET", "/admin/api/models"): _resp(200, {"data": [
            {
                "id": "Model-ABC-MLX-8bit",
                "settings": {"model_alias": "MODEL ABC ALIAS"},
            },
        ]}),
    })
    _patch_httpx(monkeypatch, router)

    from api.network_servers import router as api_router
    app = FastAPI()
    app.include_router(api_router)
    client = TestClient(app)

    r = client.get(
        "/api/settings/network-servers/srv-test/models",
        params={"token": OWNER_TOKEN},
    )
    assert r.status_code == 200, r.text
    data = r.json()

    assert len(data["loaded"]) == 1
    loaded = data["loaded"][0]
    assert loaded["id"] == "Model-ABC-MLX-8bit"
    assert loaded["model_id"] == "Model-ABC-MLX-8bit"
    assert loaded["display_name"] == "MODEL ABC ALIAS"
    assert loaded["model_alias"] == "MODEL ABC ALIAS"

    assert len(data["available"]) == 1
    avail = data["available"][0]
    assert avail["id"] == "Model-XYZ-MLX-8bit"
    assert avail["model_id"] == "Model-XYZ-MLX-8bit"
    assert avail["display_name"] == "Model-XYZ-MLX-8bit"
    for section in (data["loaded"], data["available"]):
        for m in section:
            assert m["id"] != "MODEL ABC ALIAS"
            assert m["model_id"] != "MODEL ABC ALIAS"


def test_alias_change_updates_display_name_not_pmid(monkeypatch, fresh_db):
    _stub_network_helpers(monkeypatch)
    from llm import network_discovery as nd
    monkeypatch.setattr(
        nd,
        "get_server",
        lambda sid: {
            "id": sid, "provider": "omlx",
            "instance_id": "inst-1", "instance_name": "Mac",
            "host": "127.0.0.1", "port": 5100, "status": "online",
        } if sid == "srv-test" else None,
    )

    router1 = _Router({
        ("GET", "/v1/models/status"): _resp(200, {"models": [
            {"id": "MyModel-Canonical", "loaded": True, "max_context_window": 128000},
        ]}),
        ("GET", "/admin/api/status"): _resp(200, {}),
        ("GET", "/admin/api/models"): _resp(200, {"data": [
            {"id": "MyModel-Canonical", "settings": {"model_alias": "Alias A"}},
        ]}),
    })
    _patch_httpx(monkeypatch, router1)
    from llm.catalog import discover_network_models
    asyncio.run(discover_network_models("srv-test"))

    conn = sqlite3.connect(str(fresh_db))
    conn.row_factory = sqlite3.Row
    r1 = conn.execute(
        "SELECT provider_model_id, display_name FROM models WHERE provider = 'omlx'"
    ).fetchone()
    conn.close()
    assert r1["provider_model_id"] == "MyModel-Canonical"
    assert r1["display_name"] == "Alias A"

    router2 = _Router({
        ("GET", "/v1/models/status"): _resp(200, {"models": [
            {"id": "MyModel-Canonical", "loaded": True, "max_context_window": 128000},
        ]}),
        ("GET", "/admin/api/status"): _resp(200, {}),
        ("GET", "/admin/api/models"): _resp(200, {"data": [
            {"id": "MyModel-Canonical", "settings": {"model_alias": "Alias B"}},
        ]}),
    })
    _set_router(router2)
    asyncio.run(discover_network_models("srv-test"))

    conn = sqlite3.connect(str(fresh_db))
    conn.row_factory = sqlite3.Row
    r2 = conn.execute(
        "SELECT provider_model_id, display_name FROM models WHERE provider = 'omlx'"
    ).fetchone()
    rows = conn.execute(
        "SELECT COUNT(*) FROM models WHERE provider = 'omlx'"
    ).fetchone()
    conn.close()
    assert r2["provider_model_id"] == "MyModel-Canonical"
    assert r2["display_name"] == "Alias B"
    assert rows[0] == 1


@pytest.mark.parametrize("managed", [True, False])
def test_discovery_applies_medium_only_to_corsia_managed_omlx(
    monkeypatch, fresh_db, managed,
):
    _stub_network_helpers(monkeypatch)
    monkeypatch.setattr(
        "llm.local_sources.corsia_omlx_server",
        lambda servers=None: servers[0] if managed and servers else None,
    )
    router = _Router({
        ("GET", "/v1/models/status"): _resp(200, {"models": [{
            "id": "Qwen-Managed-Test",
            "loaded": True,
            "reasoning_efforts": ["low", "medium", "xhigh"],
            "reasoning_effort_default": "low",
        }]}),
        ("GET", "/admin/api/status"): _resp(200, {}),
    })
    _patch_httpx(monkeypatch, router)

    from llm.catalog import discover_network_models

    result = asyncio.run(discover_network_models("srv-test"))

    assert result["ok"], result
    conn = sqlite3.connect(str(fresh_db))
    row = conn.execute(
        "SELECT metadata_json FROM models WHERE provider = 'omlx'"
    ).fetchone()
    conn.close()
    shape = json.loads(row[0])["reasoning_shape"]
    assert shape["default"] == ("medium" if managed else "low")
    assert ("applies_default_when_unset" in shape) is managed
