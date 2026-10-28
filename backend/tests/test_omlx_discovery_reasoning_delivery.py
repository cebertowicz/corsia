"""Discover oMLX capabilities and carry the selected effort to the provider."""

import asyncio
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest

from catalog_omlx_fixtures import _Router, _patch_httpx, _resp, _stub_network_helpers, fresh_db


@pytest.mark.parametrize("managed", [False, True])
def test_discovery_carries_selected_effort(monkeypatch, fresh_db, managed):
    from llm import network_discovery
    from llm.catalog_network_models import discover_network_models
    from llm.managed_omlx_binary import MANAGED_INSTANCE_ID
    from llm.providers.omlx_thinking import wire_reasoning_effort
    from llm.resolution import resolve_execution_target
    from ws import turn

    monkeypatch.setattr("core.agent.get_agent", AsyncMock(return_value=object()))
    monkeypatch.setattr(turn, "validate_native_target", AsyncMock())
    monkeypatch.setattr(turn, "make_provider_params", lambda _model: {})
    monkeypatch.setattr(turn, "native_provider_params", lambda _target, params: params)
    monkeypatch.setattr(turn, "make_runtime_bindings", lambda *_args: (lambda: None, lambda: None))
    monkeypatch.setattr(turn, "persist_turn_start_bound", AsyncMock(return_value=object()))
    _stub_network_helpers(monkeypatch)
    if managed:
        stub = network_discovery.get_server
        monkeypatch.setattr(network_discovery, "get_server", lambda sid: {
            **stub(sid), "kind": "managed", "source": "corsia", "instance_id": MANAGED_INSTANCE_ID,
        })
    router = _Router({
        ("GET", "/v1/models/status"): _resp(200, {"models": [
            {"id": "effort", "loaded": True},
            {"id": "toggle", "loaded": True},
            {"id": "plain", "loaded": True},
        ]}),
        ("GET", "/admin/api/status"): _resp(200, {}),
        ("GET", "/admin/api/models"): _resp(200, {"models": [
            {"id": "effort", "reasoning_efforts": ["low", "medium", "xhigh"]},
            {"id": "toggle", "thinking_default": True},
            {"id": "plain"},
        ]}),
    })
    _patch_httpx(monkeypatch, router)
    result = asyncio.run(discover_network_models("srv-test"))
    assert result["ok"], result

    with sqlite3.connect(str(fresh_db)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM models WHERE provider_model_id = 'effort'").fetchone()
        target = resolve_execution_target(row["id"], db=db)
        shape = json.loads(row["metadata_json"])["reasoning_shape"]
        selected = "low" if managed else "medium"
        prepared = asyncio.run(turn._setup_turn(
            "thread", "hello", [], target=target, turn_id="turn", ingress="ws",
            reasoning_effort=selected,
        ))
        assert wire_reasoning_effort(prepared.reasoning_effort, shape) == selected
        unset = asyncio.run(turn._setup_turn(
            "thread", "hello", [], target=target, turn_id="unset", ingress="ws",
        ))
        assert wire_reasoning_effort(unset.reasoning_effort, shape) == ("medium" if managed else None)
        flags = dict(db.execute("SELECT provider_model_id, supports_reasoning FROM models"))
        assert flags == {"effort": 1, "toggle": 1, "plain": 0}
        # Simulate an existing stale catalog row; rediscovery must repair it.
        db.execute("UPDATE models SET supports_reasoning = 0 WHERE provider_model_id = 'effort'")
        db.commit()
    result = asyncio.run(discover_network_models("srv-test"))
    assert result["updated"] == 3
    with sqlite3.connect(str(fresh_db)) as db:
        assert db.execute("SELECT supports_reasoning FROM models WHERE provider_model_id = 'effort'").fetchone()[0] == 1
