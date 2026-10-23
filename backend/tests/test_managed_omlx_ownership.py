"""Managed oMLX has one private identity; existing services and rows stay untouched."""

from __future__ import annotations

import asyncio
import os
import subprocess
from types import SimpleNamespace

import httpx
import pytest


@pytest.mark.asyncio
async def test_register_managed_server_preserves_existing_rows(isolated_db, monkeypatch, tmp_path) -> None:
    from infra.db_async import run_in_db
    from llm import network_discovery as discovery
    from llm.managed_omlx_binary import reserve_managed_omlx_port
    from llm.local_setup_runtime import LOCAL_SERVER_NAME, register_managed_server

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    legacy_managed = await run_in_db(discovery.upsert_server,
        provider="omlx", instance_id="127.0.0.1:8000", instance_name=LOCAL_SERVER_NAME,
        host="127.0.0.1", port=8000, status="online", kind="managed",
    )
    foreign_external = await run_in_db(discovery.upsert_server,
        provider="omlx", instance_id="127.0.0.1:9000", instance_name="Someone else's oMLX",
        host="127.0.0.1", port=9000, status="online", kind="external", dialect="openai",
    )
    paths, port = reserve_managed_omlx_port()

    server_id, _key = await register_managed_server(
        port=port, base_path=str(paths.base_path), model_dir=str(paths.model_dir),
    )

    registered = await run_in_db(discovery.get_server, server_id)
    assert registered is not None
    assert registered["instance_id"] == "corsia-managed"
    assert discovery.get_server_metadata(server_id)["base_path"] == str(paths.base_path)
    assert (await run_in_db(discovery.get_server, legacy_managed))["status"] == "online"
    assert (await run_in_db(discovery.get_server, foreign_external))["status"] == "online"
    # The one rule for which oMLX CORSIA may change matches only the row it registered.
    from llm.managed_omlx_binary import is_corsia_managed_server
    rows = {sid: await run_in_db(discovery.get_server, sid) for sid in (server_id, legacy_managed, foreign_external)}
    assert {sid: is_corsia_managed_server(row) for sid, row in rows.items()} == {
        server_id: True, legacy_managed: False, foreign_external: False,
    }


@pytest.mark.asyncio
async def test_register_managed_server_renames_only_its_existing_row(isolated_db, monkeypatch, tmp_path) -> None:
    from infra.db_async import run_in_db
    from infra.key_store_servers import resolve_server, set_server_key
    from llm import network_discovery as discovery
    from llm.managed_omlx_binary import MANAGED_INSTANCE_ID
    from llm.local_setup_runtime import LOCAL_SERVER_NAME, register_managed_server

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    managed = await run_in_db(
        discovery.upsert_server,
        provider="omlx", instance_id=MANAGED_INSTANCE_ID, instance_name="This Mac",
        host="127.0.0.1", port=8200, status="offline", kind="managed", source="corsia",
    )
    await run_in_db(discovery.set_server_metadata, managed, corsia_managed=True)
    legacy = await run_in_db(
        discovery.upsert_server,
        provider="omlx", instance_id="127.0.0.1:8000", instance_name="This Mac",
        host="127.0.0.1", port=8000, status="online", kind="managed", source="auto",
    )
    await run_in_db(discovery.set_server_metadata, legacy, owner="person")
    await run_in_db(set_server_key, legacy, "legacy-key")
    _model_row("legacy-model", f"omlx@{legacy}/someone/model", "someone/model")
    before = await run_in_db(discovery.get_server, legacy)
    before_key = await run_in_db(resolve_server, legacy)
    from infra.database import get_db
    db = get_db()
    before_binding = tuple(db.execute("SELECT * FROM models WHERE id = ?", ("legacy-model",)).fetchone())
    db.close()

    registered, _key = await register_managed_server(
        port=8201, base_path=str(tmp_path / "managed"), model_dir=str(tmp_path / "models"),
    )

    assert registered == managed
    assert (await run_in_db(discovery.get_server, managed))["instance_name"] == LOCAL_SERVER_NAME
    assert await run_in_db(discovery.get_server, legacy) == before
    assert await run_in_db(resolve_server, legacy) == before_key
    db = get_db()
    assert tuple(db.execute("SELECT * FROM models WHERE id = ?", ("legacy-model",)).fetchone()) == before_binding
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("old_name", ["Local", "CORSIA's own oMLX", "This Mac"])
async def test_startup_renames_an_existing_managed_row_and_its_models_only(isolated_db, monkeypatch, tmp_path, old_name) -> None:
    import json

    import app_startup_runtime
    from infra.db_async import run_in_db
    from infra.database import get_db
    from llm import local_model, network_discovery as discovery
    from llm.local_setup_runtime import LOCAL_SERVER_NAME

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    managed = await run_in_db(
        discovery.upsert_server,
        provider="omlx", instance_id="corsia-managed", instance_name=old_name,
        host="127.0.0.1", port=8200, status="online", kind="managed", source="corsia",
    )
    mine = await run_in_db(
        discovery.upsert_server,
        provider="omlx", instance_id="127.0.0.1:9000", instance_name=old_name,
        host="127.0.0.1", port=9000, status="online", kind="external", source="manual",
    )
    for model_id, server_id in (("managed-model", managed), ("my-model", mine)):
        _model_row(model_id, f"omlx@{server_id}/{model_id}", model_id)
        db = get_db()
        db.execute("UPDATE models SET metadata_json = ? WHERE id = ?",
                   (json.dumps({"server_id": server_id, "server_name": old_name}), model_id))
        db.commit()
        db.close()

    async def resumed() -> bool:
        return False

    async def notified() -> None:
        return None

    monkeypatch.setattr(local_model, "backfill_legacy_record", lambda: None)
    monkeypatch.setattr("llm.local_setup_flow.resume_pending_setup", resumed)
    monkeypatch.setattr("onboarding.local_download_wake.resume_pending_local_download", notified)

    await app_startup_runtime.resume_local_setup()

    assert LOCAL_SERVER_NAME == "CORSIA"
    assert (await run_in_db(discovery.get_server, managed))["instance_name"] == "CORSIA"
    assert (await run_in_db(discovery.get_server, mine))["instance_name"] == old_name
    db = get_db()
    names = {row["id"]: json.loads(row["metadata_json"])["server_name"]
             for row in db.execute("SELECT id, metadata_json FROM models WHERE id IN ('managed-model', 'my-model')")}
    db.close()
    assert names == {"managed-model": "CORSIA", "my-model": old_name}


def test_managed_paths_are_a_pure_lookup(monkeypatch, tmp_path) -> None:
    """Reading the paths binds no port and creates or writes nothing (startup reads them)."""
    from llm.managed_omlx_binary import managed_omlx_paths
    from llm.providers import omlx_process

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    monkeypatch.setattr(omlx_process, "port_is_free", lambda _port: pytest.fail("a path read must not bind a port"))

    paths = managed_omlx_paths()

    assert paths.model_dir == tmp_path / "managed-omlx" / "models"
    assert paths.settings_path == tmp_path / "managed-omlx" / "settings.json"
    assert list(tmp_path.iterdir()) == []


def test_port_reservation_fails_as_its_own_error_when_no_port_binds(monkeypatch, tmp_path) -> None:
    from llm.managed_omlx_binary import ManagedPortUnavailable, reserve_managed_omlx_port
    from llm.providers import omlx_process

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    monkeypatch.setattr(omlx_process, "port_is_free", lambda _port: False)

    with pytest.raises(ManagedPortUnavailable):
        reserve_managed_omlx_port()


def test_port_reservation_replaces_a_saved_foreign_port(monkeypatch, tmp_path) -> None:
    from llm import managed_omlx_binary
    from llm.providers import omlx_process, omlx_supervisor

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    paths = managed_omlx_binary.managed_omlx_paths()
    paths.base_path.mkdir()
    paths.settings_path.write_text('{"server": {"port": 8210}}')
    monkeypatch.setattr(omlx_process, "port_owner_pid", lambda *_args: 4321)
    monkeypatch.setattr(omlx_supervisor, "is_corsia_process", lambda _pid: False)
    monkeypatch.setattr(omlx_process, "port_is_free", lambda port: port == 8211)

    _paths, port = managed_omlx_binary.reserve_managed_omlx_port()

    assert port == 8211
    assert '8211' in paths.settings_path.read_text()


def test_backfill_with_no_bindable_port_records_nothing_and_does_not_raise(monkeypatch, tmp_path) -> None:
    """The L3 sandbox denies every bind; startup's record backfill must still finish."""
    from llm import local_model
    from llm.providers import omlx_process

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    monkeypatch.setattr(omlx_process, "port_is_free", lambda _port: False)

    assert local_model.backfill_legacy_record() is None
    assert not (tmp_path / "managed-omlx").exists()


@pytest.mark.asyncio
async def test_startup_local_setup_survives_a_failing_step(monkeypatch) -> None:
    import app_startup_runtime
    from llm import local_model, local_setup

    calls: list[str] = []

    def broken() -> None:
        calls.append("backfill")
        raise RuntimeError("CORSIA couldn't reserve a private port for its local model server. Try again.")

    async def resumed() -> bool:
        calls.append("resume")
        return False

    async def notified() -> None:
        calls.append("notice")

    # The failed step publishes a durable failed flight; record the call instead of writing one
    # that later tests would read.
    monkeypatch.setattr(local_setup, "record_resume_failure", lambda _ref: calls.append("failure"))
    monkeypatch.setattr(local_model, "backfill_legacy_record", broken)
    monkeypatch.setattr("llm.local_setup_flow.resume_pending_setup", resumed)
    monkeypatch.setattr("onboarding.local_download_wake.resume_pending_local_download", notified)

    await app_startup_runtime.resume_local_setup()

    assert calls == ["backfill", "failure", "resume", "notice"]


@pytest.mark.asyncio
async def test_manual_loopback_row_survives_startup_retirement_after_upgrade(isolated_db, monkeypatch, tmp_path) -> None:
    import app_startup_runtime
    from infra.db_async import run_in_db
    from llm import network_discovery as discovery
    from llm import local_model

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    manual = await run_in_db(
        discovery.upsert_server,
        provider="omlx", instance_id="127.0.0.1:9000", instance_name="My oMLX",
        host="127.0.0.1", port=9000, status="online", kind="external", source="manual",
    )

    async def resumed() -> bool:
        return False

    async def notified() -> None:
        return None

    monkeypatch.setattr(local_model, "backfill_legacy_record", lambda: None)
    monkeypatch.setattr("llm.local_setup_flow.resume_pending_setup", resumed)
    monkeypatch.setattr("onboarding.local_download_wake.resume_pending_local_download", notified)



    await app_startup_runtime.resume_local_setup()

    assert (await run_in_db(discovery.get_server, manual))["status"] == "online"



def _model_row(model_id: str, litellm_model: str, provider_model_id: str) -> None:
    from infra.database import get_db

    db = get_db()
    db.execute(
        """INSERT INTO models
           (id, provider, provider_model_id, litellm_model, display_name, backend,
            source, enabled, available, created_at, updated_at)
           VALUES (?, 'omlx', ?, ?, ?, 'network', 'discovered', 1, 1, 'now', 'now')""",
        (model_id, provider_model_id, litellm_model, model_id),
    )
    db.commit()
    db.close()


def _add_through_settings(monkeypatch, hostname: str) -> str:
    """Register an oMLX exactly as POST /api/settings/network-servers does."""
    from api import network_server_helpers
    from llm import network_discovery

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "data": [{"id": "someone/model"}]})

    real_init = httpx.AsyncClient.__init__
    monkeypatch.setattr(
        httpx.AsyncClient, "__init__",
        lambda self, *a, **k: real_init(self, *a, **{**k, "transport": httpx.MockTransport(handler)}),
    )

    async def discovered(_sid):
        return None

    monkeypatch.setattr(network_discovery, "discover_and_notify", discovered)

    async def add() -> str:
        result = await network_server_helpers._add_omlx_server(SimpleNamespace(
            hostname=hostname, port=8000, name="Studio", api_key=None,
        ))
        return str(result["server_id"])

    return asyncio.run(add())


def test_lan_omlx_added_in_settings_keeps_routing_after_retirement(isolated_db, monkeypatch, tmp_path) -> None:
    from llm import catalog, network_discovery as discovery


    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    lan = _add_through_settings(monkeypatch, "192.168.1.20")
    local = _add_through_settings(monkeypatch, "127.0.0.1")
    assert discovery.get_server(lan)["kind"] == "managed"
    assert discovery.get_server(lan)["source"] == "manual"
    _model_row("lan-model", f"omlx@{lan}/someone/model", "someone/model")
    _model_row("local-model", f"omlx@{local}/someone/local", "someone/local")



    assert discovery.get_server(lan)["status"] == "online"
    assert catalog.get_model("lan-model")["id"] == "lan-model"
    assert catalog.is_valid_model_choice(f"omlx@{lan}/someone/model") is True
    assert discovery.get_server(local)["status"] == "online"
    assert catalog.is_valid_model_choice(f"omlx@{local}/someone/local") is True



@pytest.mark.parametrize("row", [
    {"provider": "omlx", "kind": "managed", "instance_id": "192.168.1.20:8000"},
    {"provider": "omlx", "kind": "managed", "instance_id": "studio.local:8000"},
    {"provider": "omlx", "kind": "managed", "instance_id": "127.0.0.1:8000"},
    {"provider": "omlx", "kind": "external", "instance_id": "127.0.0.1:9000"},
    {"provider": "omlx", "kind": "managed", "instance_id": "corsia-managed"},
    {"provider": "ollama", "kind": "managed", "instance_id": "127.0.0.1:11434"},
])
def test_row_metadata_does_not_establish_process_ownership(row, monkeypatch, tmp_path) -> None:
    from llm.providers.omlx_supervisor import is_corsia_process

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    assert not is_corsia_process(101)


def test_supervisor_spawn_record_survives_a_new_identity_read(monkeypatch, tmp_path) -> None:
    from llm.providers import omlx_supervisor as supervisor

    monkeypatch.setattr(supervisor, "RUNTIME_DIR", tmp_path / "runtime")
    supervisor._write_pid("managed", os.getpid())
    assert supervisor._pid_path("managed").parent == tmp_path / "runtime"
    assert supervisor.is_corsia_process(os.getpid())
    supervisor._clear_pid("managed")
    assert not supervisor.is_corsia_process(os.getpid())
