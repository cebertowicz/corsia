"""A binding to a retired oMLX row runs once the managed server lists the same model.

The catalog keeps one oMLX row per model (``UNIQUE(provider, provider_model_id)``):
discovery on another server rewrites that row's transport rather than adding a
second row, so a thread bound by row id keeps its row, and one bound by the old
``omlx@<server>/<model>`` selector reaches it through the selector fallback.
"""

from __future__ import annotations

import json

import pytest

from test_managed_omlx_ownership import _add_through_settings, _model_row

MODEL = "someone/model"


def _resolve(binding: str):
    from llm.resolution import resolve_execution_target

    return resolve_execution_target(binding)


def _discover(server_id: str, provider_model_id: str) -> None:
    """Record a model the way oMLX discovery does (``_upsert_network_model``)."""
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
    from llm.local_setup_managed import MANAGED_INSTANCE_ID

    return discovery.upsert_server(
        provider="omlx", instance_id=MANAGED_INSTANCE_ID, instance_name="This Mac",
        host="127.0.0.1", port=8210, status="online", kind="managed", source="corsia",
    )


@pytest.fixture
def retired(isolated_db, monkeypatch, tmp_path) -> str:
    """A marker-gated legacy CORSIA loopback row, retired before its model is rediscovered."""
    from llm import network_discovery as discovery
    from llm.local_setup_ownership import marker_path
    from llm.local_setup_runtime import LOCAL_SERVER_NAME, retire_legacy_setup_omlx_rows

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    marker_path().write_text("legacy\n", encoding="utf-8")
    local = discovery.upsert_server(
        provider="omlx", instance_id="127.0.0.1:8000", instance_name=LOCAL_SERVER_NAME,
        host="127.0.0.1", port=8000, status="online", kind="managed", source="manual",
    )
    _model_row("local-model", f"omlx@{local}/{MODEL}", MODEL)
    assert retire_legacy_setup_omlx_rows() == [local]
    assert _resolve("local-model").available is False
    return local


def test_a_binding_to_a_retired_loopback_row_runs_on_the_managed_server(retired) -> None:
    from llm.catalog import get_model_by_litellm

    managed = _managed_server()
    _discover(managed, MODEL)

    for binding in ("local-model", f"omlx@{retired}/{MODEL}"):
        target = _resolve(binding)
        assert (target.catalog_id, target.available) == ("local-model", True)
        assert target.transport_model == f"omlx@{managed}/{MODEL}"
    assert get_model_by_litellm(f"omlx@{retired}/{MODEL}")["litellm_model"] == f"omlx@{managed}/{MODEL}"


def test_no_matching_model_on_the_managed_server_stays_unavailable(retired) -> None:
    from llm.catalog import get_model_by_litellm
    from llm.resolution import ModelUnavailableError

    managed = _managed_server()
    _discover(managed, "someone/other")

    assert _resolve("local-model").available is False
    assert get_model_by_litellm(f"omlx@{retired}/{MODEL}")["available"] == 0
    with pytest.raises(ModelUnavailableError):
        _resolve(f"omlx@{managed}/{MODEL}")


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
