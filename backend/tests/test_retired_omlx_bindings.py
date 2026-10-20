"""Existing oMLX bindings survive registration of a separate managed server."""

from __future__ import annotations

import json

import pytest

from test_managed_omlx_ownership import _add_through_settings, _model_row

MODEL = "someone/model"


def _resolve(binding: str):
    from llm.resolution import resolve_execution_target
    return resolve_execution_target(binding)


def _discover(server_id: str, provider_model_id: str) -> None:
    from infra.database import get_db
    from llm.catalog_network_models import _upsert_network_model

    db = get_db()
    _upsert_network_model(db, "omlx", {
        "provider_model_id": provider_model_id,
        "litellm_model": f"omlx@{server_id}/{provider_model_id}",
        "display_name": provider_model_id, "description": None,
        "context_window": 32768, "supports_vision": 0, "metadata_json": json.dumps({}),
    }, "now")
    db.commit()
    db.close()


def _managed_server() -> str:
    from llm import network_discovery as discovery
    from llm.managed_omlx_binary import MANAGED_INSTANCE_ID

    return discovery.upsert_server(
        provider="omlx", instance_id=MANAGED_INSTANCE_ID, instance_name="This Mac",
        host="127.0.0.1", port=8210, status="online", kind="managed", source="corsia",
    )


@pytest.mark.parametrize("state", ["offline", "disabled"])
def test_an_offline_or_disabled_lan_server_keeps_its_bindings(isolated_db, monkeypatch, tmp_path, state) -> None:
    from llm import catalog, network_discovery as discovery

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    lan = _add_through_settings(monkeypatch, "192.168.1.20")
    _model_row("lan-model", f"omlx@{lan}/{MODEL}", MODEL)
    managed = _managed_server()
    _discover(managed, "someone/starter")
    if state == "offline":
        discovery.update_server_status(lan, status="offline")
    else:
        assert discovery.set_server_enabled(lan, False)
    assert discovery.get_server(lan)["status"] == state
    for binding in ("lan-model", f"omlx@{lan}/{MODEL}"):
        target = _resolve(binding)
        assert (target.catalog_id, target.transport_model) == ("lan-model", f"omlx@{lan}/{MODEL}")
    assert catalog.get_model_by_litellm(f"omlx@{lan}/{MODEL}")["litellm_model"] == f"omlx@{lan}/{MODEL}"


@pytest.mark.asyncio
async def test_old_setup_binding_key_and_markers_survive_new_registration(isolated_db, monkeypatch, tmp_path):
    from infra.db_async import run_in_db
    from infra.key_store_servers import resolve_server, set_server_key
    from llm import network_discovery as discovery
    from llm.local_setup_runtime import register_managed_server
    from llm.managed_omlx_binary import managed_omlx_paths

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    marker = tmp_path / "local_setup_installed_omlx"
    marker.write_text("legacy")
    old = await run_in_db(discovery.upsert_server, provider="omlx", instance_id="127.0.0.1:8000",
                          instance_name="This Mac", host="127.0.0.1", port=8000,
                          status="online", kind="managed", source="manual")
    await run_in_db(set_server_key, old, "old-test-key")
    await run_in_db(_model_row, "old-model", f"omlx@{old}/{MODEL}", MODEL)
    paths = managed_omlx_paths()
    await register_managed_server(port=8210, base_path=str(paths.base_path), model_dir=str(paths.model_dir))
    assert (await run_in_db(discovery.get_server, old))["status"] == "online"
    assert await run_in_db(resolve_server, old) == "old-test-key"
    assert (await run_in_db(_resolve, "old-model")).transport_model == f"omlx@{old}/{MODEL}"
    assert marker.read_text() == "legacy"
