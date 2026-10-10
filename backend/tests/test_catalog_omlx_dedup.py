
from __future__ import annotations

import asyncio
import sqlite3

import httpx
import pytest

from catalog_omlx_fixtures import (
    _Router,
    _patch_httpx,
    _resp,
    _stub_network_helpers,
    fresh_db,
)


def test_aliased_models_become_one_row_keyed_on_canonical(monkeypatch, fresh_db):
    _stub_network_helpers(monkeypatch)
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
    })
    _patch_httpx(monkeypatch, router)

    from llm.catalog import discover_network_models
    result = asyncio.run(discover_network_models("srv-test"))

    assert result["ok"], result
    assert result["discovered"] == 2
    assert result["updated"] == 0

    conn = sqlite3.connect(str(fresh_db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT provider_model_id, display_name FROM models WHERE provider = 'omlx' ORDER BY provider_model_id"
    ).fetchall()
    conn.close()
    assert len(rows) == 2
    pmids = [r["provider_model_id"] for r in rows]
    assert "Model-ABC-MLX-8bit" in pmids
    assert "Model-XYZ-MLX-8bit" in pmids
    assert "MODEL ABC ALIAS" not in pmids
    assert "MODEL XYZ ALIAS" not in pmids


def test_alias_promoted_to_display_name(monkeypatch, fresh_db):
    _stub_network_helpers(monkeypatch)
    from llm import network_discovery as nd
    monkeypatch.setattr(
        nd,
        "get_server",
        lambda sid: {
            "id": sid, "provider": "omlx",
            "instance_id": "inst-1", "instance_name": "Mac",
            "host": "127.0.0.1", "port": 5100, "status": "online",
            "api_key": "admin-key",
        } if sid == "srv-test" else None,
    )
    router = _Router({
        ("GET", "/v1/models/status"): _resp(200, {"models": [
            {
                "id": "Model-ABC-MLX-8bit",
                "loaded": True,
                "engine_type": "llm",
                "max_context_window": 128000,
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

    from llm.catalog import discover_network_models
    result = asyncio.run(discover_network_models("srv-test"))

    assert result["ok"], result
    conn = sqlite3.connect(str(fresh_db))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT provider_model_id, display_name FROM models WHERE provider = 'omlx'"
    ).fetchone()
    conn.close()
    assert row["provider_model_id"] == "Model-ABC-MLX-8bit"
    assert row["display_name"] == "MODEL ABC ALIAS"


def test_canonical_id_unchanged_when_alias_unset(monkeypatch, fresh_db):
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
    asyncio.run(discover_network_models("srv-test"))

    conn = sqlite3.connect(str(fresh_db))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT provider_model_id, display_name FROM models WHERE provider = 'omlx'"
    ).fetchone()
    conn.close()
    assert row["provider_model_id"] == "Model-XYZ-MLX-8bit"
    assert row["display_name"] == "Model-XYZ-MLX-8bit"



def test_stale_alias_rows_are_hard_deleted(monkeypatch, fresh_db):
    _stub_network_helpers(monkeypatch)

    from infra.database import get_db
    db = get_db()
    db.execute(
        """INSERT INTO models (id, provider, provider_model_id, litellm_model,
           display_name, source, created_at, updated_at)
           VALUES ('vmlx-alias', 'omlx', 'MODEL ABC ALIAS',
                   'omlx@srv-test/MODEL ABC ALIAS', 'MODEL ABC ALIAS',
                   'discovered', '2026-05-19T00:00:00Z', '2026-05-19T00:00:00Z')"""
    )
    db.execute(
        """INSERT INTO models (id, provider, provider_model_id, litellm_model,
           display_name, source, created_at, updated_at)
           VALUES ('vmlx-phantom', 'omlx', 'Model-ABC-MLX-4bit',
                   'omlx@srv-test/Model-ABC-MLX-4bit',
                   'Model-ABC-MLX-4bit', 'discovered',
                   '2026-05-19T00:00:00Z', '2026-05-19T00:00:00Z')"""
    )
    db.commit()
    db.close()

    router = _Router({
        ("GET", "/v1/models/status"): _resp(200, {"models": [
            {
                "id": "Model-ABC-MLX-8bit",
                "loaded": True,
                "engine_type": "llm",
                "max_context_window": 128000,
            },
        ]}),
        ("GET", "/admin/api/status"): _resp(200, {"device": "mlx"}),
    })
    _patch_httpx(monkeypatch, router)

    from llm.catalog import discover_network_models
    result = asyncio.run(discover_network_models("srv-test"))

    assert result["ok"], result
    assert result["missing"] == 2  # alias + phantom both pruned

    conn = sqlite3.connect(str(fresh_db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT provider_model_id FROM models WHERE provider = 'omlx' ORDER BY provider_model_id"
    ).fetchall()
    conn.close()
    pmids = [r["provider_model_id"] for r in rows]
    assert pmids == ["Model-ABC-MLX-8bit"]


def test_non_discovered_stale_rows_soft_marked_not_deleted(monkeypatch, fresh_db):
    _stub_network_helpers(monkeypatch)

    from infra.database import get_db
    db = get_db()
    db.execute(
        """INSERT INTO models (id, provider, provider_model_id, litellm_model,
           display_name, source, created_at, updated_at)
           VALUES ('vmlx-manual', 'omlx', 'manual-model-1',
                   'omlx@srv-test/manual-model-1', 'manual-model-1',
                   'manual', '2026-05-19T00:00:00Z', '2026-05-19T00:00:00Z')"""
    )
    db.commit()
    db.close()

    router = _Router({
        ("GET", "/v1/models/status"): _resp(200, {"models": []}),
        ("GET", "/admin/api/status"): _resp(200, {"device": "mlx"}),
    })
    _patch_httpx(monkeypatch, router)

    from llm.catalog import discover_network_models
    asyncio.run(discover_network_models("srv-test"))

    conn = sqlite3.connect(str(fresh_db))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, provider_model_id, available, source FROM models WHERE provider = 'omlx'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["id"] == "vmlx-manual"
    assert row["available"] == 0
    assert row["source"] == "manual"


@pytest.mark.parametrize(
    "status_response",
    [
        _resp(503),
        httpx.Response(
            200,
            content=b"{not-json",
            request=httpx.Request("GET", "http://x/v1/models/status"),
        ),
    ],
)
def test_failed_status_discovery_preserves_existing_catalog_rows(
    monkeypatch,
    fresh_db,
    status_response,
):
    """A failed authoritative snapshot cannot mean every model disappeared."""
    _stub_network_helpers(monkeypatch)

    from infra.database import get_db

    db = get_db()
    db.execute(
        """INSERT INTO models (id, provider, provider_model_id, litellm_model,
           display_name, source, created_at, updated_at)
           VALUES ('vmlx-existing', 'omlx', 'Fortissimo',
                   'omlx@srv-test/Fortissimo', 'Fortissimo', 'discovered',
                   '2026-08-31T00:00:00Z', '2026-08-31T00:00:00Z')"""
    )
    db.commit()
    db.close()

    router = _Router({("GET", "/v1/models/status"): status_response})
    _patch_httpx(monkeypatch, router)

    from llm.catalog import discover_network_models

    result = asyncio.run(discover_network_models("srv-test"))

    assert result["ok"] is False
    assert result["missing"] == 0
    conn = sqlite3.connect(str(fresh_db))
    row = conn.execute(
        "SELECT id, available FROM models WHERE id = 'vmlx-existing'"
    ).fetchone()
    conn.close()
    assert row == ("vmlx-existing", 1)


def test_stale_context_window_reconciled_from_v1_models(monkeypatch, fresh_db):
    """A stale stored window is corrected to the live /v1/models max_model_len (#788).

    The status payload omits a window here, so the authoritative runtime window
    is only reachable via /v1/models. The existing row must be updated to it.
    """
    _stub_network_helpers(monkeypatch)

    from infra.database import get_db
    db = get_db()
    db.execute(
        """INSERT INTO models (id, provider, provider_model_id, litellm_model,
           display_name, source, context_window, created_at, updated_at)
           VALUES ('vmlx-stale', 'omlx', 'Model-27B-oQ8-mtp',
                   'omlx@srv-test/Model-27B-oQ8-mtp', 'Model-27B-oQ8-mtp',
                   'discovered', 40960,
                   '2026-05-19T00:00:00Z', '2026-05-19T00:00:00Z')"""
    )
    db.commit()
    db.close()

    # /v1/models/status must be registered before /v1/models so the substring
    # router resolves the status URL to the status route.
    router = _Router({
        ("GET", "/v1/models/status"): _resp(200, {"models": [
            {"id": "Model-27B-oQ8-mtp", "loaded": True, "engine_type": "llm"},
        ]}),
        ("GET", "/v1/models"): _resp(200, {"data": [
            {"id": "Model-27B-oQ8-mtp", "max_model_len": 262144},
        ]}),
        ("GET", "/admin/api/status"): _resp(200, {"device": "mlx"}),
    })
    _patch_httpx(monkeypatch, router)

    from llm.catalog import discover_network_models
    result = asyncio.run(discover_network_models("srv-test"))
    assert result["ok"], result

    conn = sqlite3.connect(str(fresh_db))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT context_window FROM models "
        "WHERE provider_model_id = 'Model-27B-oQ8-mtp'"
    ).fetchone()
    conn.close()
    assert row["context_window"] == 262144


def test_v1_models_runtime_window_resolves_by_alias(monkeypatch, fresh_db):
    """/v1/models keys a loaded model by its alias; the runtime window still lands."""
    _stub_network_helpers(monkeypatch)

    from infra.database import get_db
    db = get_db()
    db.execute(
        """INSERT INTO models (id, provider, provider_model_id, litellm_model,
           display_name, source, context_window, created_at, updated_at)
           VALUES ('vmlx-alias-win', 'omlx', 'Model-27B-oQ8-mtp',
                   'omlx@srv-test/Model-27B-oQ8-mtp', 'Model-27B-oQ8-mtp',
                   'discovered', 40960,
                   '2026-05-19T00:00:00Z', '2026-05-19T00:00:00Z')"""
    )
    db.commit()
    db.close()

    router = _Router({
        ("GET", "/v1/models/status"): _resp(200, {"models": [
            {"id": "Model-27B-oQ8-mtp", "loaded": True, "engine_type": "llm",
             "model_alias": "Cadenza"},
        ]}),
        ("GET", "/v1/models"): _resp(200, {"data": [
            {"id": "Cadenza", "max_model_len": 262144},
        ]}),
        ("GET", "/admin/api/status"): _resp(200, {"device": "mlx"}),
    })
    _patch_httpx(monkeypatch, router)

    from llm.catalog import discover_network_models
    result = asyncio.run(discover_network_models("srv-test"))
    assert result["ok"], result

    conn = sqlite3.connect(str(fresh_db))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT context_window FROM models "
        "WHERE provider_model_id = 'Model-27B-oQ8-mtp'"
    ).fetchone()
    conn.close()
    assert row["context_window"] == 262144


def test_config_base_does_not_shadow_runtime_window(monkeypatch, fresh_db):
    """Admin's model-config base window must not shadow the status runtime window."""
    _stub_network_helpers(monkeypatch)
    from llm import network_discovery as nd
    monkeypatch.setattr(
        nd,
        "get_server",
        lambda sid: {
            "id": sid, "provider": "omlx",
            "instance_id": "inst-1", "instance_name": "Mac",
            "host": "127.0.0.1", "port": 5100, "status": "online",
            "api_key": "[REDACTED]",
        } if sid == "srv-test" else None,
    )

    from infra.database import get_db
    db = get_db()
    db.execute(
        """INSERT INTO models (id, provider, provider_model_id, litellm_model,
           display_name, source, context_window, created_at, updated_at)
           VALUES ('vmlx-shadow', 'omlx', 'Model-27B-oQ8-mtp',
                   'omlx@srv-test/Model-27B-oQ8-mtp', 'Model-27B-oQ8-mtp',
                   'discovered', 40960,
                   '2026-05-19T00:00:00Z', '2026-05-19T00:00:00Z')"""
    )
    db.commit()
    db.close()

    router = _Router({
        ("GET", "/v1/models/status"): _resp(200, {"models": [
            {"id": "Model-27B-oQ8-mtp", "loaded": True, "engine_type": "llm",
             "max_context_window": 262144},
        ]}),
        ("GET", "/admin/api/status"): _resp(200, {"device": "mlx"}),
        ("GET", "/admin/api/models"): _resp(200, {"data": [
            {"id": "Model-27B-oQ8-mtp",
             "config": {"max_position_embeddings": 40960}},
        ]}),
    })
    _patch_httpx(monkeypatch, router)

    from llm.catalog import discover_network_models
    result = asyncio.run(discover_network_models("srv-test"))
    assert result["ok"], result

    conn = sqlite3.connect(str(fresh_db))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT context_window FROM models "
        "WHERE provider_model_id = 'Model-27B-oQ8-mtp'"
    ).fetchone()
    conn.close()
    assert row["context_window"] == 262144
