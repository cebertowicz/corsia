"""Network discovery respects oMLX's per-instance is_hidden flag.

A hidden model is not advertised by its instance, so it must neither be
inserted fresh, claim/overwrite a shared row from another instance serving
the same physical model, nor linger after a same-server hide transition.
"""

from __future__ import annotations

import asyncio
import sqlite3

from catalog_omlx_fixtures import (
    _Router,
    _patch_httpx,
    _resp,
    _stub_network_helpers,
    fresh_db,
)


def test_hidden_model_is_not_cataloged(monkeypatch, fresh_db):
    """oMLX's per-instance is_hidden flag must keep a model out of the catalog.

    A hidden model is not advertised by this instance, so it must neither be
    inserted fresh nor claim/overwrite a shared row that another instance
    already bound to itself.
    """
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
                "id": "Model-HIDDEN-8bit",
                "loaded": False,
                "engine_type": "llm",
                "max_context_window": 128000,
                "estimated_size": 50_000_000_000,
                "is_hidden": True,
            },
        ]}),
        ("GET", "/admin/api/status"): _resp(200, {"device": "mlx"}),
    })
    _patch_httpx(monkeypatch, router)

    from llm.catalog import discover_network_models
    result = asyncio.run(discover_network_models("srv-test"))

    assert result["ok"], result
    assert result["discovered"] == 1
    assert result["updated"] == 0

    conn = sqlite3.connect(str(fresh_db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT provider_model_id FROM models WHERE provider = 'omlx'"
    ).fetchall()
    conn.close()
    assert [r["provider_model_id"] for r in rows] == ["Model-ABC-MLX-8bit"]


def test_hidden_model_does_not_overwrite_existing_row(monkeypatch, fresh_db):
    """A hidden model must not steal a catalog row bound to another server.

    Server B hides the same model Server A serves. Discovery on B must leave
    the existing row (bound to A's litellm_model) untouched — otherwise the
    two-instance shared-pool case regresses into last-discoverer-wins.
    """
    conn = sqlite3.connect(str(fresh_db))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """INSERT INTO models
           (id, provider, provider_model_id, litellm_model, display_name,
            backend, source, enabled, available, created_at, updated_at)
           VALUES ('vmlx-existing', 'omlx', 'Model-HIDDEN-8bit',
                   'omlx@ns-serverA/Model-HIDDEN-8bit', 'Hidden Model',
                   'network', 'discovered', 1, 1, '2026-01-01', '2026-01-01')"""
    )
    conn.commit()
    conn.close()

    _stub_network_helpers(monkeypatch)
    router = _Router({
        ("GET", "/v1/models/status"): _resp(200, {"models": [
            {
                "id": "Model-HIDDEN-8bit",
                "loaded": True,
                "engine_type": "llm",
                "max_context_window": 128000,
                "estimated_size": 50_000_000_000,
                "is_hidden": True,
            },
        ]}),
        ("GET", "/admin/api/status"): _resp(200, {"device": "mlx"}),
    })
    _patch_httpx(monkeypatch, router)

    from llm.catalog import discover_network_models
    result = asyncio.run(discover_network_models("srv-test"))

    assert result["ok"], result
    assert result["discovered"] == 0
    assert result["updated"] == 0

    conn = sqlite3.connect(str(fresh_db))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT litellm_model, available FROM models WHERE provider_model_id = 'Model-HIDDEN-8bit'"
    ).fetchone()
    conn.close()
    assert row["litellm_model"] == "omlx@ns-serverA/Model-HIDDEN-8bit"
    assert row["available"] == 1


def test_hidden_after_discovery_same_server_is_reaped(monkeypatch, fresh_db):
    """A model hidden on THIS server after first discovery is retired by the
    stale-row sweep, not left as an enabled/available row.

    Discovery previously cataloged Model-HIDDEN on srv-test. The operator then
    hides it on srv-test. The next discovery sees no such model, so it must not
    appear in seen_pmids; the same-server stale sweep then deletes the
    discovered row (EAS8 regression).
    """
    _stub_network_helpers(monkeypatch)

    from infra.database import get_db
    db = get_db()
    db.execute(
        """INSERT INTO models (id, provider, provider_model_id, litellm_model,
           display_name, source, created_at, updated_at)
           VALUES ('vmlx-hidden-later', 'omlx', 'Model-HIDDEN-8bit',
                   'omlx@srv-test/Model-HIDDEN-8bit', 'Hidden Model',
                   'discovered', '2026-05-19T00:00:00Z', '2026-05-19T00:00:00Z')"""
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
            {
                "id": "Model-HIDDEN-8bit",
                "loaded": True,
                "engine_type": "llm",
                "max_context_window": 128000,
                "is_hidden": True,
            },
        ]}),
        ("GET", "/admin/api/status"): _resp(200, {"device": "mlx"}),
    })
    _patch_httpx(monkeypatch, router)

    from llm.catalog import discover_network_models
    result = asyncio.run(discover_network_models("srv-test"))

    assert result["ok"], result
    assert result["discovered"] == 1
    assert result["missing"] == 1  # hidden-later row reaped

    conn = sqlite3.connect(str(fresh_db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT provider_model_id FROM models WHERE provider = 'omlx'"
    ).fetchall()
    conn.close()
    assert [r["provider_model_id"] for r in rows] == ["Model-ABC-MLX-8bit"]