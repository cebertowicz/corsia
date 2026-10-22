from __future__ import annotations

from llm import local_downloads, local_setup, local_sources
from llm.local_model import LocalModel


def test_settings_downloads_are_separate_from_setup_marker(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(local_downloads, "_path", lambda: tmp_path / "local-downloads.json")
    model = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768, "Qwen 3.8 27B")

    local_downloads.write(local_downloads.Download(model, speed=52.0, server_id="local-server"))

    stored = local_downloads.get(model.model)
    assert stored is not None
    assert stored.model == model
    assert stored.speed == 52.0
    assert stored.view() == {
        "name": "Qwen 3.8 27B", "repository": model.repository, "model": model.model,
        "download_size_bytes": 1_000_000, "download_size_gb": 0.0,
        "context_window": 32_768, "status": "available", "speed": 52.0,
        "detail": None, "server_id": "local-server",
    }
    local_downloads.write(local_downloads.Download(model, status="failed", detail="network unavailable"))
    failed = local_downloads.get(model.model)
    assert failed is not None and failed.status == "failed" and failed.detail == "network unavailable"
    local_downloads.forget(model.model)
    assert local_downloads.all() == []


def test_unreadable_record_is_kept_aside_and_not_overwritten(monkeypatch, tmp_path) -> None:
    target = tmp_path / "local-downloads.json"
    monkeypatch.setattr(local_downloads, "_path", lambda: target)
    target.write_text("{not json", encoding="utf-8")
    model = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768, "Qwen 3.8 27B")

    assert local_downloads.all() == []
    local_downloads.write(local_downloads.Download(model, server_id="local-server"))

    assert (tmp_path / "local-downloads.json.corrupt").read_text(encoding="utf-8") == "{not json"
    assert [entry.model for entry in local_downloads.all()] == [model]


def test_failed_settings_download_projects_a_durable_retry_target(monkeypatch, tmp_path) -> None:
    model = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768, "Qwen 3.8 27B")
    monkeypatch.setattr(local_downloads, "_path", lambda: tmp_path / "local-downloads.json")
    local_downloads.write(local_downloads.Download(model, status="failed", detail="network unavailable"))
    monkeypatch.setattr(local_sources, "_omlx_state", lambda _membership, _servers=None: {"state": "ready"})
    monkeypatch.setattr(local_setup, "setup_kind", lambda: "settings")
    monkeypatch.setattr(local_setup, "setup_stage", lambda: None)
    # The source reflects only CORSIA's managed local oMLX row.
    monkeypatch.setattr(local_sources, "get_servers", lambda **_kwargs: [])

    source = local_sources.omlx_source((set(), set()))

    assert source["settings_download"] == {
        "name": "Qwen 3.8 27B", "repository": model.repository, "percent": None, "failed": True, "downloaded": False,
    }


def test_a_download_past_its_bytes_projects_as_finishing(monkeypatch) -> None:
    model = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768, "Qwen 3.8 27B")
    monkeypatch.setattr(local_sources, "_omlx_state", lambda _membership, _servers=None: {"state": "ready"})
    monkeypatch.setattr(local_setup, "setup_kind", lambda: "settings")
    monkeypatch.setattr(local_setup, "flight_model", lambda: model)
    monkeypatch.setattr(local_sources, "get_servers", lambda **_kwargs: [])
    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
    monkeypatch.setattr(local_setup, "_running", lambda: True)
    shown = []
    # The phase is what the caller passes, never read back from the stage wording.
    for stage, finishing in (("Loading model", False), ("Downloading (100%)", False), ("Any wording", True)):
        local_setup.report_stage(stage, finishing=finishing)
        shown.append(local_sources.omlx_source((set(), set()))["settings_download"]["finishing"])

    assert shown == [False, False, True]


# --- the flight after the bytes land ---------------------------------------------------------

def _flight_world(monkeypatch, tmp_path, *, server_status: str):
    """A real server row in the test DB, a fake download that ends at once, and a fake oMLX listing."""
    from llm import catalog_network_models, local_setup, local_setup_operation, local_settings_download
    from llm import network_discovery as nd
    from llm.local_setup_runtime import OptimalResult

    monkeypatch.setattr(local_downloads, "_path", lambda: tmp_path / "local-downloads.json")
    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
    monkeypatch.setattr(local_setup_operation, "_state_root", lambda: tmp_path)
    monkeypatch.setattr(local_setup, "_emit", lambda *_args, **_kwargs: None)
    model = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768, "Qwen 3.8 27B")
    server_id = nd.upsert_server("omlx", "flight-instance", "This Mac", "127.0.0.1", 8210, server_status, source="corsia")
    listed: list[list[str]] = []

    async def downloaded(_report, _model):
        return server_id

    async def settings(*_args, **_kwargs):
        return OptimalResult("offline")

    async def fetch(*_args, **_kwargs):
        return [{"id": model.model}], {}

    def reconcile(_server_id, _server, canonical_models, _status, _now, result):
        listed.append([row["id"] for row in canonical_models])
        result["ok"] = True
        return result

    monkeypatch.setattr(local_settings_download, "_managed_download", downloaded)
    monkeypatch.setattr("llm.local_setup_runtime.apply_optimal_settings", settings)
    monkeypatch.setattr("llm.local_setup_runtime.configure_model_settings", settings)
    monkeypatch.setattr("llm.network_endpoints.build_api_base", lambda _sid: "http://127.0.0.1:8210/v1")
    monkeypatch.setattr("llm.network_endpoints.build_mgmt_base", lambda _sid: "http://127.0.0.1:8210/api")
    monkeypatch.setattr(catalog_network_models, "_fetch_omlx_canonical_models", fetch)
    monkeypatch.setattr(catalog_network_models, "_reconcile_network_models", reconcile)

    async def no_keep_loaded(*_args, **_kwargs):
        return None

    monkeypatch.setattr(local_settings_download, "keep_loaded", no_keep_loaded)
    return model, server_id, listed


def test_a_server_marked_offline_mid_download_is_listed_with_no_failed_record(monkeypatch, tmp_path) -> None:
    """The sweep can flag a busy oMLX offline while it finalizes; the download that just used it says otherwise."""
    import asyncio

    from llm import local_settings_download
    from llm import network_discovery as nd

    model, server_id, listed = _flight_world(monkeypatch, tmp_path, server_status="offline")

    result = asyncio.run(local_settings_download._run_flight(model))

    assert result["ok"] is True
    assert listed == [[model.model]]
    assert nd.get_server(server_id)["status"] == "online"
    record = local_downloads.get(model.model)
    assert record is not None and record.status == "available"
    assert not [entry for entry in local_downloads.all() if entry.status == "failed"]


def test_the_steps_after_the_bytes_land_report_the_finishing_phase(monkeypatch, tmp_path) -> None:
    import asyncio

    from llm import local_settings_download

    model, _server_id, _listed = _flight_world(monkeypatch, tmp_path, server_status="online")
    reported: list[tuple[str, bool]] = []
    monkeypatch.setattr(local_setup, "report_stage", lambda stage, *, finishing=False: reported.append((stage, finishing)))

    asyncio.run(local_settings_download._run_flight(model))

    assert reported == [("Refreshing model catalog", True), ("Loading model", True)]


def test_a_disabled_server_is_never_revived_by_the_download(monkeypatch, tmp_path) -> None:
    import asyncio

    from llm import local_settings_download
    from llm import network_discovery as nd

    model, server_id, _listed = _flight_world(monkeypatch, tmp_path, server_status="disabled")

    asyncio.run(local_settings_download._run_flight(model))

    assert nd.get_server(server_id)["status"] == "disabled"


def test_a_refresh_that_really_fails_says_the_model_is_downloaded(monkeypatch, tmp_path) -> None:
    import asyncio

    from llm import local_settings_download, local_sources

    model, _server_id, _listed = _flight_world(monkeypatch, tmp_path, server_status="online")

    async def broken(*_args, **_kwargs):
        raise OSError("connection reset")

    monkeypatch.setattr("llm.catalog_network_models._fetch_omlx_canonical_models", broken)

    result = asyncio.run(local_settings_download._run_flight(model))

    assert result["ok"] is False and result["downloaded"] is True
    assert result["message"] == "Qwen 3.8 27B is downloaded but couldn’t be added. Retry to finish adding it."
    record = local_downloads.get(model.model)
    assert record.status == "failed" and record.downloaded is True
    monkeypatch.setattr(local_sources, "_omlx_state", lambda _membership, _servers=None: {"state": "ready"})
    monkeypatch.setattr(local_sources, "get_servers", lambda **_kwargs: [])
    monkeypatch.setattr("llm.local_setup.setup_kind", lambda: "settings")
    monkeypatch.setattr("llm.local_setup.setup_stage", lambda: None)
    assert local_sources.omlx_source((set(), set()))["settings_download"]["downloaded"] is True


def _failed_record_world(monkeypatch, tmp_path, *, downloaded: bool) -> LocalModel:
    from contextlib import nullcontext

    from llm import catalog_network_models

    model = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 1_000_000, 32_768, "Qwen 3.8 27B")
    monkeypatch.setattr(local_downloads, "_path", lambda: tmp_path / "local-downloads.json")
    local_downloads.write(local_downloads.Download(model, status="failed", detail="refresh failed", downloaded=downloaded))
    monkeypatch.setattr(local_sources, "_omlx_state", lambda _membership, _servers=None: {"state": "ready"})
    monkeypatch.setattr(local_sources, "get_servers", lambda **_kwargs: [{"id": "corsia-server"}, {"id": "own-server"}])
    monkeypatch.setattr(local_sources, "is_corsia_managed_server", lambda server: server["id"] == "corsia-server")
    monkeypatch.setattr("llm.local_setup.setup_kind", lambda: "settings")
    monkeypatch.setattr("llm.local_setup.setup_stage", lambda: None)
    # Discovery's listing write, without a database: only the settle step after it is under test.
    monkeypatch.setattr(catalog_network_models, "write_transaction", lambda *_a, **_k: nullcontext())
    monkeypatch.setattr(catalog_network_models, "_network_model_payload", lambda *_a: {})
    monkeypatch.setattr(catalog_network_models, "_upsert_network_model", lambda *_a: "updated")
    monkeypatch.setattr(catalog_network_models, "_reconcile_stale_network_models", lambda *_a: 0)
    return model


def _discovery_lists(server_id: str, listed: list[str]) -> None:
    from llm import catalog_network_models

    catalog_network_models._reconcile_network_models(
        server_id, {"provider": "omlx"}, [{"id": name} for name in listed], {}, "now",
        {"discovered": 0, "updated": 0, "missing": 0})


def test_a_failed_record_shows_until_discovery_of_corsias_omlx_lists_its_model(monkeypatch, tmp_path) -> None:
    for downloaded in (True, False):
        model = _failed_record_world(monkeypatch, tmp_path, downloaded=downloaded)

        # Neither another model on CORSIA's oMLX nor the same model on a person's own oMLX settles it.
        _discovery_lists("corsia-server", ["Other-Model-oQ4e"])
        _discovery_lists("own-server", [model.model])
        shown = local_sources.omlx_source((set(), set()))["settings_download"]
        assert shown["failed"] is True and shown["downloaded"] is downloaded
        assert local_downloads.get(model.model) is not None

        _discovery_lists("corsia-server", [model.model])
        assert local_downloads.get(model.model) is None
        assert "settings_download" not in local_sources.omlx_source((set(), set()))


def test_a_download_that_never_finished_still_says_it_could_not_download(monkeypatch, tmp_path) -> None:
    import asyncio

    from llm import local_settings_download

    model, _server_id, _listed = _flight_world(monkeypatch, tmp_path, server_status="online")

    async def failed(_report, _model):
        raise RuntimeError("network unavailable")

    monkeypatch.setattr(local_settings_download, "_managed_download", failed)

    result = asyncio.run(local_settings_download._run_flight(model))

    assert result["message"] == "Couldn't download Qwen 3.8 27B. Try again."
    assert local_downloads.get(model.model).downloaded is False


def test_a_setup_download_projects_like_a_settings_download(monkeypatch) -> None:
    model = LocalModel("Jundot/DeepSeek-V4-Flash-0731", 1_000_000, 32_768, "DeepSeek V4 Flash 0731")
    monkeypatch.setattr(local_sources, "_omlx_state", lambda _membership, _servers=None: {"state": "ready", "local_state": "downloading"})
    monkeypatch.setattr(local_setup, "setup_kind", lambda: "setup")
    monkeypatch.setattr(local_setup, "flight_model", lambda: model)
    monkeypatch.setattr(local_setup, "setup_stage", lambda: "Downloading DeepSeek-V4-Flash-0731 (1 MB, 12%)")
    monkeypatch.setattr(local_setup, "download_percent", lambda: 12)
    monkeypatch.setattr(local_sources, "get_servers", lambda **_kwargs: [])

    source = local_sources.omlx_source((set(), set()))

    assert source["settings_download"] == {
        "name": "DeepSeek V4 Flash 0731", "repository": model.repository, "size_bytes": 1_000_000,
        "percent": 12, "failed": False, "finishing": False,
    }


def test_a_setup_that_has_not_picked_a_model_projects_no_download(monkeypatch) -> None:
    monkeypatch.setattr(local_sources, "_omlx_state", lambda _membership, _servers=None: {"state": "ready", "local_state": "starting"})
    monkeypatch.setattr(local_setup, "setup_kind", lambda: "setup")
    monkeypatch.setattr(local_setup, "flight_model", lambda: None)
    monkeypatch.setattr(local_setup, "setup_stage", lambda: "Starting")
    monkeypatch.setattr(local_sources, "get_servers", lambda **_kwargs: [])

    assert "settings_download" not in local_sources.omlx_source((set(), set()))


def test_finishing_a_setup_flight_nudges_open_screens(monkeypatch) -> None:
    nudges: list[int] = []
    monkeypatch.setattr("infra.state.ws.schedule_models_changed", lambda: nudges.append(1))
    monkeypatch.setattr(local_setup.local_setup_operation, "read", lambda: None)
    monkeypatch.setattr(local_setup, "_release_operation_lock", lambda: None)

    local_setup.finish_flight({"ok": False})

    assert nudges == [1]


def _stopped_server_with_flight(monkeypatch, flight_state: str) -> dict:
    monkeypatch.setattr(local_sources, "local_memory_ok", lambda: True)
    monkeypatch.setattr("llm.local_setup_prepare.known_model", lambda: None)
    monkeypatch.setattr(local_setup, "setup_kind", lambda: "setup")
    monkeypatch.setattr("onboarding.local_download_state.current", lambda: {"state": flight_state})
    monkeypatch.setattr(local_sources, "local_omlx_servers", lambda _rows=None: [{"id": "s1", "status": "offline", "updated_at": None}])
    return local_sources._omlx_state((set(), set()))


def test_a_finished_setup_whose_server_was_turned_off_reads_stopped(monkeypatch) -> None:
    """Turning the Ready local server off must not read as a setup still finishing."""
    result = _stopped_server_with_flight(monkeypatch, "ready")

    assert result["reason_code"] == "not_running"
    assert result["local_state"] == "stopped"


def test_a_failed_setup_on_a_stopped_server_still_reads_failed(monkeypatch) -> None:
    assert _stopped_server_with_flight(monkeypatch, "start_failed")["local_state"] == "start_failed"
