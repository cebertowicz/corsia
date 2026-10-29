from __future__ import annotations

import pytest
from fastapi import HTTPException

from api import provider_local_models
from llm import local_downloads, local_setup_operation
from llm.local_model import LocalModel


pytestmark = pytest.mark.usefixtures("mac_local_model_host")

MODEL = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768, level=4.0, fp16=False, relative_speed=0.85)


class _Response:
    def __init__(self, status: int, body: dict) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> dict:
        return self._body


async def _server_in_db(function, *args, **kwargs):
    return function(*args, **kwargs)


async def _no_loaded(_config):
    return []


def _omlx_rows(monkeypatch, servers: list[dict], managed: frozenset[str] | set[str] = frozenset()) -> None:
    """Rows the resolver sees; only ids in ``managed`` are CORSIA's own oMLX."""
    from llm import local_sources

    monkeypatch.setattr(local_sources, "get_servers", lambda **_kwargs: servers)
    monkeypatch.setattr(local_sources, "is_corsia_managed_server", lambda server: server["id"] in managed)
    monkeypatch.setattr(provider_local_models, "run_in_db", _server_in_db)


def _corsia_server(monkeypatch, servers: list[dict]) -> None:
    _omlx_rows(monkeypatch, servers, {server["id"] for server in servers})


@pytest.mark.asyncio
async def test_server_refuses_a_persons_own_omlx_without_an_admin_call(monkeypatch) -> None:
    _omlx_rows(monkeypatch, [{"id": "studio", "status": "online", "source": "discovered"}])
    called = False

    async def recommendations(_server_id):
        nonlocal called
        called = True
        return {"best": [MODEL]}

    monkeypatch.setattr(provider_local_models, "local_memory_ok", lambda: True)
    monkeypatch.setattr(provider_local_models, "ask_recommendations", recommendations)

    with pytest.raises(HTTPException) as raised:
        await provider_local_models._recommendations()

    assert raised.value.status_code == 409
    assert raised.value.detail == "Local models aren’t set up. Select Set up to continue."
    assert not called


@pytest.mark.asyncio
async def test_server_selects_corsias_own_omlx_over_a_persons_own(monkeypatch) -> None:
    studio = {"id": "studio", "status": "online", "source": "discovered"}
    corsia = {"id": "corsia-omlx", "status": "online"}
    _omlx_rows(monkeypatch, [studio, corsia], {"corsia-omlx"})

    assert await provider_local_models._server() == corsia


@pytest.mark.asyncio
async def test_offline_managed_omlx_is_not_selected(monkeypatch) -> None:
    _omlx_rows(monkeypatch, [{"id": "corsia-omlx", "status": "offline"}], {"corsia-omlx"})

    with pytest.raises(HTTPException) as raised:
        await provider_local_models._server()

    # Set up is hidden beside CORSIA's own offline row, so the fix named is the row's own Start.
    assert raised.value.detail == "CORSIA’s local model server isn’t running. Open its settings and select Start."


@pytest.mark.parametrize("status", ["online", "offline"])
@pytest.mark.asyncio
async def test_every_consumer_resolves_corsias_own_omlx_alike(monkeypatch, status) -> None:
    """Identity holds online or offline; consumers that need it running read status."""
    from api import network_servers
    from llm import local_sources

    rows = [{"id": "studio", "status": "online"}, {"id": "corsia", "status": status}]
    _omlx_rows(monkeypatch, rows, {"corsia"})
    monkeypatch.setattr(local_sources, "_omlx_state", lambda _membership=None, _servers=None: {})
    monkeypatch.setattr(local_sources, "catalog_membership", lambda: ({"omlx@corsia", "omlx@studio"}, set()))

    assert local_sources.corsia_omlx_server()["id"] == "corsia"
    source = local_sources.omlx_source()
    assert source["corsia_omlx"] is True
    assert source["corsia_omlx_online"] is (status == "online")
    assert (local_sources.connected_local_server() or {}).get("id") == ("corsia" if status == "online" else None)

    async def run_in_db(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(network_servers.nd, "get_servers", lambda: [dict(row, provider="omlx") for row in rows])
    monkeypatch.setattr(network_servers, "run_in_db", run_in_db)
    monkeypatch.setattr(network_servers, "_network_view", lambda _server_id: {})
    monkeypatch.setattr(network_servers, "billing_views", lambda ids: {server_id: {} for server_id in ids})
    monkeypatch.setattr(network_servers, "server_keys", lambda _ids: {})
    monkeypatch.setattr(network_servers, "_public_server_view", lambda server, _keys: server)
    listed = (await network_servers.list_servers())["servers"]
    assert {server["id"]: server["corsia_omlx"] for server in listed} == {"studio": False, "corsia": True}


def test_a_turned_off_corsia_omlx_is_not_resolved(monkeypatch) -> None:
    from llm import local_sources

    _omlx_rows(monkeypatch, [{"id": "corsia", "status": "disabled"}], {"corsia"})

    assert local_sources.corsia_omlx_server() is None


async def _known_disk_model(_config):
    return [{"identifier": MODEL.model}]


def _route_with_known_model(monkeypatch) -> None:
    _corsia_server(monkeypatch, [{"id": "server", "status": "online"}])
    monkeypatch.setattr("llm.providers.omlx_lifecycle.list_loaded", _no_loaded)
    monkeypatch.setattr("llm.providers.omlx_lifecycle.list_disk_models", _known_disk_model)
    monkeypatch.setattr("llm.local_model.set_up_model", lambda: None)
    monkeypatch.setattr(provider_local_models.local_setup_operation, "read", lambda: None)


@pytest.mark.asyncio
async def test_delete_known_omlx_model_without_settings_record(monkeypatch, tmp_path) -> None:
    _route_with_known_model(monkeypatch)
    monkeypatch.setattr(local_downloads, "_path", lambda: tmp_path / "local-downloads.json")
    requests: list[tuple[str, str]] = []

    async def admin_request(_config, method, path, **_kwargs):
        requests.append((method, path))
        return _Response(200, {} if method == "DELETE" else {"models": []})

    async def discovered(_server_id):
        return {"ok": True}

    changed: list[bool] = []
    emitted: list[tuple[str, dict]] = []
    monkeypatch.setattr("llm.providers.omlx_endpoints.admin_request", admin_request)
    monkeypatch.setattr("llm.catalog_network_models.discover_network_models", discovered)
    monkeypatch.setattr("llm.local_setup._emit", lambda action, **properties: emitted.append((action, properties)))
    monkeypatch.setattr("infra.state.ws.notify_models_changed", lambda: changed.append(True))

    assert await provider_local_models.delete_download(MODEL.repository, {}) == {"ok": True}
    assert requests == [("DELETE", f"/hf/models/{MODEL.model}"), ("GET", "/models")]
    assert emitted == [("deleted", {"kind": "settings", "model": MODEL.model})]
    assert changed == [True]


@pytest.mark.asyncio
async def test_delete_clears_an_available_record_missing_from_disk(monkeypatch, tmp_path) -> None:
    _route_with_known_model(monkeypatch)
    monkeypatch.setattr(local_downloads, "_path", lambda: tmp_path / "local-downloads.json")
    local_downloads.write(local_downloads.Download(MODEL))

    async def missing_from_disk(_config):
        return []

    changed: list[bool] = []
    monkeypatch.setattr("llm.providers.omlx_lifecycle.list_disk_models", missing_from_disk)
    monkeypatch.setattr("infra.state.ws.notify_models_changed", lambda: changed.append(True))

    assert await provider_local_models.delete_download(MODEL.repository, {}) == {"ok": True}
    assert local_downloads.get(MODEL.model) is None
    assert changed == [True]


@pytest.mark.asyncio
async def test_delete_removes_durable_settings_record_after_catalog_confirms_absence(monkeypatch, tmp_path) -> None:
    _route_with_known_model(monkeypatch)
    monkeypatch.setattr(local_downloads, "_path", lambda: tmp_path / "local-downloads.json")
    local_downloads.write(local_downloads.Download(MODEL))

    async def admin_request(_config, method, _path, **_kwargs):
        return _Response(200, {} if method == "DELETE" else {"models": []})

    async def discovered(_server_id):
        return {"ok": True}

    monkeypatch.setattr("llm.providers.omlx_endpoints.admin_request", admin_request)
    monkeypatch.setattr("llm.catalog_network_models.discover_network_models", discovered)
    monkeypatch.setattr("infra.state.ws.notify_models_changed", lambda: None)

    assert await provider_local_models.delete_download(MODEL.repository, {}) == {"ok": True}
    assert local_downloads.get(MODEL.model) is None


@pytest.mark.asyncio
async def test_delete_keeps_durable_row_until_catalog_confirms_absence(monkeypatch, tmp_path) -> None:
    _route_with_known_model(monkeypatch)
    monkeypatch.setattr(local_downloads, "_path", lambda: tmp_path / "local-downloads.json")
    local_downloads.write(local_downloads.Download(MODEL))

    async def admin_request(_config, method, _path, **_kwargs):
        return _Response(200, {} if method == "DELETE" else {"models": [{"id": MODEL.model}]})

    async def discovered(_server_id):
        return {"ok": True}

    monkeypatch.setattr("llm.providers.omlx_endpoints.admin_request", admin_request)
    monkeypatch.setattr("llm.catalog_network_models.discover_network_models", discovered)

    with pytest.raises(HTTPException, match="Couldn't delete"):
        await provider_local_models.delete_download(MODEL.repository, {})

    assert local_downloads.get(MODEL.model) is not None


@pytest.mark.asyncio
async def test_delete_refuses_loaded_model(monkeypatch) -> None:
    _route_with_known_model(monkeypatch)

    async def loaded(_config):
        return [{"identifier": MODEL.model}]

    monkeypatch.setattr("llm.providers.omlx_lifecycle.list_loaded", loaded)

    with pytest.raises(HTTPException, match="Couldn't delete this model while it is loaded") as raised:
        await provider_local_models.delete_download(MODEL.repository, {})

    assert raised.value.status_code == 409


@pytest.mark.asyncio
async def test_delete_refuses_actual_corsia_setup_model(monkeypatch) -> None:
    _route_with_known_model(monkeypatch)
    monkeypatch.setattr("llm.local_model.set_up_model", lambda: MODEL)

    with pytest.raises(HTTPException) as raised:
        await provider_local_models.delete_download(MODEL.repository, {})

    assert raised.value.status_code == 409
    assert raised.value.detail == "Couldn't delete this model: CORSIA keeps it to run when no other model is available."


@pytest.mark.asyncio
async def test_delete_refuses_active_or_recoverable_operation_for_model(monkeypatch) -> None:
    _route_with_known_model(monkeypatch)
    monkeypatch.setattr(
        provider_local_models.local_setup_operation,
        "read",
        lambda: local_setup_operation.Operation.for_model(MODEL, "settings", "resume-task"),
    )

    with pytest.raises(HTTPException, match="Couldn't delete this model while it is downloading") as raised:
        await provider_local_models.delete_download(MODEL.repository, {})

    assert raised.value.status_code == 409


@pytest.mark.asyncio
async def test_recommendations_remove_available_records_missing_from_disk(monkeypatch, tmp_path) -> None:
    async def server():
        return {"id": "server"}

    async def missing_from_disk(_config):
        return []

    failed_model = LocalModel("Jundot/Other", 1_000_000, 32_768)
    monkeypatch.setattr(local_downloads, "_path", lambda: tmp_path / "local-downloads.json")
    local_downloads.write(local_downloads.Download(MODEL))
    local_downloads.write(local_downloads.Download(failed_model, status="failed"))
    monkeypatch.setattr(provider_local_models, "local_memory_ok", lambda: True)
    monkeypatch.setattr(provider_local_models, "_server", server)
    monkeypatch.setattr(provider_local_models, "omlx_has_recommender", lambda _version: False)
    monkeypatch.setattr(provider_local_models, "omlx_version", lambda: "0.7.0")
    monkeypatch.setattr("llm.providers.omlx_lifecycle.read_models_status", missing_from_disk)

    result, _sections = await provider_local_models._recommendations()

    assert result["downloads"] == [local_downloads.Download(failed_model, status="failed").view()]
    assert local_downloads.get(MODEL.model) is None
    assert local_downloads.get(failed_model.model) is not None


@pytest.mark.asyncio
async def test_recommendations_leave_records_when_disk_listing_fails(monkeypatch, tmp_path) -> None:
    async def server():
        return {"id": "server"}

    async def unavailable_disk_listing(_config):
        raise RuntimeError("offline")

    monkeypatch.setattr(local_downloads, "_path", lambda: tmp_path / "local-downloads.json")
    local_downloads.write(local_downloads.Download(MODEL))
    monkeypatch.setattr(provider_local_models, "local_memory_ok", lambda: True)
    monkeypatch.setattr(provider_local_models, "_server", server)
    monkeypatch.setattr(provider_local_models, "omlx_has_recommender", lambda _version: False)
    monkeypatch.setattr(provider_local_models, "omlx_version", lambda: "0.7.0")
    monkeypatch.setattr("llm.providers.omlx_lifecycle.list_disk_models", unavailable_disk_listing)

    result, _sections = await provider_local_models._recommendations()

    assert result["downloads"] == [local_downloads.Download(MODEL).view()]
    assert local_downloads.get(MODEL.model) is not None


def _ready_recommender(monkeypatch, tmp_path, sections: dict[str, list[LocalModel]], room_gib: int) -> None:
    async def recommended(_server_id):
        return sections

    async def server():
        return {"id": "server"}

    async def model_dir(_server_id):
        return tmp_path

    async def no_disk_models(_config):
        return []

    monkeypatch.setattr(provider_local_models, "local_memory_ok", lambda: True)
    monkeypatch.setattr(provider_local_models, "host_ram_bytes", lambda: 64 * 1024**3)
    monkeypatch.setattr(provider_local_models, "pin_room_bytes", lambda: room_gib * 1024**3)
    monkeypatch.setattr(provider_local_models, "omlx_has_recommender", lambda _version: True)
    monkeypatch.setattr(provider_local_models, "omlx_version", lambda: "0.7.1")
    monkeypatch.setattr(provider_local_models, "_server", server)
    monkeypatch.setattr("llm.providers.omlx_lifecycle.read_models_status", no_disk_models)
    monkeypatch.setattr(provider_local_models, "ask_recommendations", recommended)
    monkeypatch.setattr(provider_local_models, "download_dir", model_dir)
    monkeypatch.setattr(
        provider_local_models,
        "download_disk_space",
        lambda *_args: {"state": "ready", "free_bytes": 20 * 1000**3},
    )


@pytest.mark.asyncio
async def test_recommendations_include_memory_and_download_volume_free_disk(monkeypatch, tmp_path) -> None:
    _ready_recommender(monkeypatch, tmp_path, {"best": [MODEL]}, room_gib=22)

    result, _sections = await provider_local_models._recommendations()

    assert result["memory_gb"] == 64
    assert result["memory_room_gb"] == 22
    assert result["disk_free_gb"] == 20
    assert result["recommended_repository"] == MODEL.repository
    assert result["models"] == [{
        **MODEL.view(), "disk": {"state": "ready", "free_bytes": 20 * 1000**3},
        "on_disk": False, "section": "best",
    }]


@pytest.mark.asyncio
async def test_recommended_row_is_first_fitting_best_list_build(monkeypatch, tmp_path) -> None:
    huge = LocalModel("Jundot/DeepSeek-V4-Flash", 1_000_000, 32_768, resident_bytes=163 * 1024**3)
    small = LocalModel("Jundot/Qwen3.6-35B-A3B", 1_000_000, 32_768, resident_bytes=40 * 1024**3)
    _ready_recommender(monkeypatch, tmp_path, {"best": [huge, small]}, room_gib=68)

    result, _sections = await provider_local_models._recommendations()

    assert result["recommended_repository"] == small.repository


@pytest.mark.asyncio
async def test_recommended_skips_unavailable_build_but_keeps_owned_build(monkeypatch, tmp_path) -> None:
    first = LocalModel("Jundot/first", 1_000_000, 32_768, resident_bytes=10 * 1024**3)
    second = LocalModel("Jundot/second", 1_000_000, 32_768, resident_bytes=10 * 1024**3)
    _ready_recommender(monkeypatch, tmp_path, {"best": [first, second]}, room_gib=22)
    monkeypatch.setattr(provider_local_models, "download_disk_space", lambda *_args: {"state": "insufficient"})

    async def owned(_config):
        return [{"id": second.model, "loaded": False, "estimated_size": 0}]

    monkeypatch.setattr("llm.providers.omlx_lifecycle.read_models_status", owned)
    result, _sections = await provider_local_models._recommendations()
    assert result["recommended_repository"] == second.repository
    assert next(row for row in result["models"] if row["repository"] == second.repository)["on_disk"] is True


@pytest.mark.asyncio
async def test_owning_another_build_does_not_change_recommendation(monkeypatch, tmp_path) -> None:
    first = LocalModel("Jundot/first", 1_000_000, 32_768, resident_bytes=10 * 1024**3)
    second = LocalModel("Jundot/second", 1_000_000, 32_768, resident_bytes=10 * 1024**3)
    _ready_recommender(monkeypatch, tmp_path, {"best": [first, second]}, room_gib=22)

    async def owned(_config):
        return [{"id": second.model, "loaded": False, "estimated_size": 0}]

    monkeypatch.setattr("llm.providers.omlx_lifecycle.read_models_status", owned)
    result, _sections = await provider_local_models._recommendations()
    assert result["recommended_repository"] == first.repository


@pytest.mark.asyncio
async def test_unknown_disk_remains_eligible(monkeypatch, tmp_path) -> None:
    _ready_recommender(monkeypatch, tmp_path, {"best": [MODEL]}, room_gib=22)
    monkeypatch.setattr(provider_local_models, "download_disk_space", lambda *_args: {"state": "unknown"})
    result, _ = await provider_local_models._recommendations()
    assert result["recommended_repository"] == MODEL.repository


@pytest.mark.asyncio
async def test_status_failure_is_visible(monkeypatch, tmp_path) -> None:
    _ready_recommender(monkeypatch, tmp_path, {"best": [MODEL]}, room_gib=22)

    async def unavailable(_config):
        return None

    monkeypatch.setattr("llm.providers.omlx_lifecycle.read_models_status", unavailable)
    with pytest.raises(HTTPException) as raised:
        await provider_local_models._recommendations()
    assert raised.value.status_code == 503
    assert raised.value.detail == "Couldn’t check which models are on this computer."


@pytest.mark.asyncio
async def test_no_recommended_row_when_nothing_fits(monkeypatch, tmp_path) -> None:
    huge = LocalModel("Jundot/DeepSeek-V4-Flash", 1_000_000, 32_768, resident_bytes=163 * 1024**3)
    big = LocalModel("Jundot/Qwen3.6-35B-A3B", 1_000_000, 32_768, resident_bytes=40 * 1024**3)
    _ready_recommender(monkeypatch, tmp_path, {"best": [huge], "faster": [big]}, room_gib=9)

    result, _sections = await provider_local_models._recommendations()

    assert result["models"] == []
    assert result["recommended_repository"] is None


@pytest.mark.asyncio
async def test_no_recommended_row_without_a_model(monkeypatch, tmp_path) -> None:
    _ready_recommender(monkeypatch, tmp_path, {"best": [], "unlock": [MODEL]}, room_gib=68)

    result, _sections = await provider_local_models._recommendations()

    assert result["recommended_repository"] is None


@pytest.mark.asyncio
async def test_download_names_a_model_missing_from_recommendations(monkeypatch) -> None:
    async def no_recommendations():
        return {}, {"best": []}

    monkeypatch.setattr(provider_local_models, "_recommendations", no_recommendations)
    monkeypatch.setattr(provider_local_models, "_clear_failed_record", lambda _model_id: None)

    with pytest.raises(HTTPException) as raised:
        await provider_local_models.download_model({"repository": MODEL.repository}, {})

    assert raised.value.status_code == 400
    assert raised.value.detail == "Couldn't download that model: it isn't in the list for this computer right now. Pick another from the list."


@pytest.mark.asyncio
async def test_download_resumes_the_same_model_operation(monkeypatch) -> None:
    operation = local_setup_operation.Operation.for_model(MODEL, "settings", "resume-task")
    monkeypatch.setattr(provider_local_models, "_recommendations", lambda: _recommendations_for(MODEL))
    monkeypatch.setattr(provider_local_models, "_server", lambda: _model_server())
    monkeypatch.setattr(provider_local_models, "download_dir", lambda _server_id: _model_dir())
    monkeypatch.setattr(provider_local_models, "download_disk_space", lambda *_args: {"state": "ready"})
    monkeypatch.setattr(provider_local_models.local_setup_operation, "read", lambda: operation)
    resumed: list[LocalModel] = []

    async def begin(model):
        resumed.append(model)
        return {"ok": True, "resumed": True}

    monkeypatch.setattr("llm.local_setup_flow.begin_settings_download", begin)

    assert await provider_local_models.download_model({"repository": MODEL.repository}, {}) == {"ok": True, "resumed": True}
    assert resumed == [MODEL]


@pytest.mark.asyncio
async def test_download_refuses_another_model_operation(monkeypatch) -> None:
    other = LocalModel("Jundot/Other", 1_000_000, 32_768)
    monkeypatch.setattr(provider_local_models, "_recommendations", lambda: _recommendations_for(MODEL))
    monkeypatch.setattr(provider_local_models, "_server", lambda: _model_server())
    monkeypatch.setattr(provider_local_models, "download_dir", lambda _server_id: _model_dir())
    monkeypatch.setattr(provider_local_models, "download_disk_space", lambda *_args: {"state": "ready"})
    monkeypatch.setattr(
        provider_local_models.local_setup_operation,
        "read",
        lambda: local_setup_operation.Operation.for_model(other, "settings", "other-task"),
    )

    with pytest.raises(HTTPException, match="Other is already downloading"):
        await provider_local_models.download_model({"repository": MODEL.repository}, {})


async def _recommendations_for(model: LocalModel):
    return {}, {"best": [model]}


async def _model_server():
    return {"id": "server"}


async def _model_dir():
    from pathlib import Path

    return Path("/tmp")


_TERMINAL_RESULTS = [
    {"ok": True, "state": "ready"},
    {"ok": False, "state": "stopped", "cause": "cancelled"},
    {"ok": False, "state": "failed", "cause": "other"},
]


def _operation_with(model: LocalModel, result: dict | None) -> local_setup_operation.Operation:
    return local_setup_operation.replace(local_setup_operation.Operation.for_model(model, "settings", "task"), result=result)


def _route_a_download(monkeypatch) -> None:
    monkeypatch.setattr(provider_local_models, "_recommendations", lambda: _recommendations_for(MODEL))
    monkeypatch.setattr(provider_local_models, "_server", lambda: _model_server())
    monkeypatch.setattr(provider_local_models, "download_dir", lambda _server_id: _model_dir())
    monkeypatch.setattr(provider_local_models, "download_disk_space", lambda *_args: {"state": "ready"})


@pytest.mark.parametrize("result", _TERMINAL_RESULTS)
@pytest.mark.asyncio
async def test_a_terminal_operation_blocks_neither_another_download_nor_its_own_delete(monkeypatch, result, tmp_path) -> None:
    _route_a_download(monkeypatch)
    started: list[LocalModel] = []

    async def begin(model):
        started.append(model)
        return {"ok": True}

    monkeypatch.setattr("llm.local_setup_flow.begin_settings_download", begin)
    other = LocalModel("Jundot/Other", 1_000_000, 32_768)
    monkeypatch.setattr(provider_local_models.local_setup_operation, "read", lambda: _operation_with(other, result))
    assert await provider_local_models.download_model({"repository": MODEL.repository}, {}) == {"ok": True}
    assert started == [MODEL]

    _route_with_known_model(monkeypatch)
    monkeypatch.setattr(local_downloads, "_path", lambda: tmp_path / "local-downloads.json")
    monkeypatch.setattr(provider_local_models.local_setup_operation, "read", lambda: _operation_with(MODEL, result))

    async def admin_request(_config, method, path, **_kwargs):
        return _Response(200, {} if method == "DELETE" else {"models": []})

    async def discovered(_server_id):
        return {"ok": True}

    monkeypatch.setattr("llm.providers.omlx_endpoints.admin_request", admin_request)
    monkeypatch.setattr("llm.catalog_network_models.discover_network_models", discovered)
    monkeypatch.setattr("llm.local_setup._emit", lambda action, **properties: None)
    monkeypatch.setattr("infra.state.ws.notify_models_changed", lambda: None)
    assert await provider_local_models.delete_download(MODEL.repository, {}) == {"ok": True}


@pytest.mark.parametrize("result", [None, {"ok": True, "downloading": True}])
@pytest.mark.asyncio
async def test_an_in_flight_operation_still_blocks_another_download_and_its_own_delete(monkeypatch, result) -> None:
    _route_a_download(monkeypatch)
    other = LocalModel("Jundot/Other", 1_000_000, 32_768)
    monkeypatch.setattr(provider_local_models.local_setup_operation, "read", lambda: _operation_with(other, result))
    with pytest.raises(HTTPException, match="Other is already downloading"):
        await provider_local_models.download_model({"repository": MODEL.repository}, {})

    _route_with_known_model(monkeypatch)
    monkeypatch.setattr(provider_local_models.local_setup_operation, "read", lambda: _operation_with(MODEL, result))
    with pytest.raises(HTTPException, match="while it is downloading"):
        await provider_local_models.delete_download(MODEL.repository, {})


@pytest.mark.asyncio
async def test_models_are_unique_fitting_rows_in_omlx_order(monkeypatch, tmp_path) -> None:
    def row(name, smarts, speed, gib):
        return LocalModel(f"Jundot/{name}", 1_000_000, 32_768, resident_bytes=gib * 1024**3,
                          smarts_level=smarts, tokens_per_second=speed)

    slow_smart = row("slow-smart", 4.0, 20.0, 10)
    fast_smart = row("fast-smart", 4.0, 60.0, 10)
    plain = row("plain", 2.0, 90.0, 10)
    too_big = row("too-big", 5.0, 30.0, 50)
    # A model oMLX lists in two sections appears once; ``unlock`` rows are not offered.
    # oMLX's own first fitting row leads; the remaining fitting rows retain its order.
    sections = {"best": [too_big, slow_smart], "faster": [plain, fast_smart, slow_smart], "unlock": [row("unlock", 5.0, 9.0, 200)]}
    _ready_recommender(monkeypatch, tmp_path, sections, room_gib=22)

    result, _sections = await provider_local_models._recommendations()

    assert [m["repository"] for m in result["models"]] == [
        "Jundot/slow-smart", "Jundot/plain", "Jundot/fast-smart",
    ]
    assert all("fits_now" not in model for model in result["models"])
    assert result["recommended_repository"] == "Jundot/slow-smart"
