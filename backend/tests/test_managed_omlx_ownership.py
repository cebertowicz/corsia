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
    from llm import local_model

    calls: list[str] = []

    def broken() -> None:
        calls.append("backfill")
        raise RuntimeError("CORSIA couldn't reserve a private port for its local model server. Try again.")

    async def resumed() -> bool:
        calls.append("resume")
        return False

    async def notified() -> None:
        calls.append("notice")

    monkeypatch.setattr(local_model, "backfill_legacy_record", broken)
    monkeypatch.setattr("llm.local_setup_new_omlx.resume_pending_setup", resumed)
    monkeypatch.setattr("onboarding.local_download_wake.resume_pending_local_download", notified)



    await app_startup_runtime.resume_local_setup()

    assert calls == ["backfill", "resume", "notice"]


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
    monkeypatch.setattr("llm.local_setup_new_omlx.resume_pending_setup", resumed)
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


# Process identity is produced by managed; the choice owner executes its exact set.

_PS = "  101  1 /opt/homebrew/opt/omlx/bin/omlx serve --port 8000\n"


def _fake_commands(monkeypatch, *, launchctl_list: str, record: list[list[str]]) -> None:
    from llm import managed_omlx_binary as managed

    def run(command, **_kwargs):
        record.append(list(command))
        if command[:2] == ["ps", "-axo"]:
            return subprocess.CompletedProcess(command, 0, _PS, "")
        if command == ["launchctl", "list"]:
            return subprocess.CompletedProcess(command, 0, launchctl_list, "")
        if command[:2] == ["launchctl", "bootout"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"unexpected command {command}")

    monkeypatch.setattr(managed.subprocess, "run", run)
    monkeypatch.setattr(managed.os, "getuid", lambda: 501)
    monkeypatch.setattr(managed.psutil, "process_iter", lambda _attrs: [SimpleNamespace(info={
        "pid": 101, "ppid": 1, "name": "Python", "cmdline": _PS.split()[2:],
        "uids": SimpleNamespace(real=501), "status": "running",
    })])



def test_stop_mine_boots_out_a_launchd_job_for_this_session_only(monkeypatch) -> None:
    from llm import local_setup_foreign as foreign, managed_omlx_binary as managed

    commands: list[list[str]] = []
    _fake_commands(monkeypatch, launchctl_list="PID\tStatus\tLabel\n101\t0\thomebrew.mxcl.omlx\n", record=commands)
    monkeypatch.setattr(foreign.os, "kill", lambda *_a: pytest.fail("a launchd job must not be signalled"))
    monkeypatch.setattr(foreign.os, "getuid", lambda: 501)

    processes = managed.foreign_omlx_processes()
    assert processes is not None and [p.pid for p in processes] == [101]
    assert foreign.stop_foreign_omlx_processes(processes) == "stopped"
    assert ["launchctl", "bootout", "gui/501/homebrew.mxcl.omlx"] in commands
    assert not any(command[0] in {"brew", "rm"} or "unload" in command or "remove" in command
                   for command in commands)


def test_stop_mine_signals_a_plain_process(monkeypatch) -> None:
    from llm import local_setup_foreign as foreign, managed_omlx_binary as managed

    commands: list[list[str]] = []
    signals: list[tuple[int, int]] = []
    _fake_commands(monkeypatch, launchctl_list="PID\tStatus\tLabel\n-\t0\tcom.example.other\n", record=commands)
    monkeypatch.setattr(foreign.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    processes = managed.foreign_omlx_processes()
    assert processes is not None
    assert foreign.stop_foreign_omlx_processes(processes) == "stopped"
    assert signals == [(101, 15)]
    assert not any(command[:2] == ["launchctl", "bootout"] for command in commands)


def test_stop_mine_falls_back_to_a_signal_when_launchd_is_unreadable(monkeypatch) -> None:
    from llm import local_setup_foreign as foreign, managed_omlx_binary as managed

    signals: list[int] = []

    def run(command, **_kwargs):
        if command[:2] == ["ps", "-axo"]:
            return subprocess.CompletedProcess(command, 0, _PS, "")
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(managed.subprocess, "run", run)
    monkeypatch.setattr(managed.os, "getuid", lambda: 501)
    monkeypatch.setattr(managed.psutil, "process_iter", lambda _attrs: [SimpleNamespace(info={
        "pid": 101, "ppid": 1, "name": "Python", "cmdline": _PS.split()[2:],
        "uids": SimpleNamespace(real=501), "status": "running",
    })])

    monkeypatch.setattr(foreign.os, "kill", lambda pid, _sig: signals.append(pid))

    processes = managed.foreign_omlx_processes()
    assert processes is not None
    assert foreign.stop_foreign_omlx_processes(processes) == "stopped"
    assert signals == [101]


def test_registered_legacy_brew_service_is_never_offered_to_stop(isolated_db, monkeypatch, tmp_path) -> None:
    from llm import local_setup_foreign as foreign, managed_omlx_binary as managed, network_discovery as discovery
    from llm import local_sources

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    (tmp_path / "local_setup_installed_omlx").write_text("legacy\n")
    sid = discovery.upsert_server(provider="omlx", instance_id="127.0.0.1:8000", instance_name="This Mac",
                                  host="127.0.0.1", port=8000, status="online", kind="managed")
    _model_row("legacy-model", f"omlx@{sid}/someone/model", "someone/model")
    _fake_commands(monkeypatch, launchctl_list="PID\tStatus\tLabel\n101\t0\thomebrew.mxcl.omlx\n", record=[])
    monkeypatch.setattr("llm.providers.omlx_process.port_owner_pid", lambda host, port: 101)
    monkeypatch.setattr(foreign.os, "kill", lambda *_args: pytest.fail("registered server must never stop"))
    assert managed.foreign_omlx_processes() == ()
    assert foreign.apply_foreign_omlx_choice() is None
    assert foreign.apply_foreign_omlx_choice(other_omlx="stop") is None
    assert local_sources.omlx_source()["state"] == "ready"
    assert not foreign.foreign_omlx_choice_path().exists()


def test_stop_child_omlx_never_unloads_its_parent_app(monkeypatch) -> None:
    from llm import local_setup_foreign as foreign, managed_omlx_binary as managed

    commands, signals = [], []
    _fake_commands(monkeypatch, launchctl_list="PID\tStatus\tLabel\n99\t0\tcom.example.editor\n", record=commands)
    monkeypatch.setattr(managed.psutil, "process_iter", lambda _attrs: [SimpleNamespace(info={
        "pid": 101, "ppid": 99, "name": "omlx", "cmdline": _PS.split()[2:],
        "uids": SimpleNamespace(real=501), "status": "running",
    })])
    monkeypatch.setattr(foreign.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    processes = managed.foreign_omlx_processes()
    assert processes is not None and processes[0].label is None
    assert foreign.stop_foreign_omlx_processes(processes) == "stopped"
    assert signals == [(101, 15)]
    assert not any(command[:2] == ["launchctl", "bootout"] for command in commands)
