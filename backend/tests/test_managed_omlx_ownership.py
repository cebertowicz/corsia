"""Managed oMLX has one private state-root identity; unowned local rows retire."""

from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

import httpx
import pytest


@pytest.mark.asyncio
async def test_register_managed_server_retires_only_legacy_managed_row(isolated_db, monkeypatch, tmp_path) -> None:
    from infra.db_async import run_in_db
    from llm import network_discovery as discovery
    from llm.local_setup_managed import is_corsia_managed_server, reserve_managed_omlx_port
    from llm.local_setup_ownership import marker_path
    from llm.local_setup_runtime import LOCAL_SERVER_NAME, register_managed_server

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    marker_path().write_text("legacy\n", encoding="utf-8")
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
    assert is_corsia_managed_server(registered)
    assert (await run_in_db(discovery.get_server, legacy_managed))["status"] == "removed"
    assert (await run_in_db(discovery.get_server, foreign_external))["status"] == "online"


def test_managed_paths_are_a_pure_lookup(monkeypatch, tmp_path) -> None:
    """Reading the paths binds no port and creates or writes nothing (startup reads them)."""
    from llm.local_setup_managed import managed_omlx_paths
    from llm.providers import omlx_process

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    monkeypatch.setattr(omlx_process, "port_is_free", lambda _port: pytest.fail("a path read must not bind a port"))

    paths = managed_omlx_paths()

    assert paths.model_dir == tmp_path / "managed-omlx" / "models"
    assert paths.settings_path == tmp_path / "managed-omlx" / "settings.json"
    assert list(tmp_path.iterdir()) == []


def test_port_reservation_fails_as_its_own_error_when_no_port_binds(monkeypatch, tmp_path) -> None:
    from llm.local_setup_managed import ManagedPortUnavailable, reserve_managed_omlx_port
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
    from llm import local_model, local_setup_upgrade

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
    monkeypatch.setattr("llm.local_setup_recovery.resume_pending_setup", resumed)
    monkeypatch.setattr("onboarding.local_download_wake.resume_pending_local_download", notified)
    monkeypatch.setattr("llm.local_setup_upgrade.done_path", lambda: SimpleNamespace(exists=lambda: True))
    monkeypatch.setattr("llm.local_setup_runtime.retire_legacy_setup_omlx_rows", lambda: calls.append("retire"))
    monkeypatch.setattr(local_setup_upgrade, "start_upgrade", lambda: calls.append("upgrade"))

    await app_startup_runtime.resume_local_setup()

    assert calls == ["retire", "backfill", "resume", "notice", "upgrade"]


@pytest.mark.asyncio
async def test_manual_loopback_row_survives_startup_retirement_after_upgrade(isolated_db, monkeypatch, tmp_path) -> None:
    import app_startup_runtime
    from infra.db_async import run_in_db
    from llm import network_discovery as discovery
    from llm import local_model, local_setup_upgrade
    from llm.local_setup_ownership import marker_path

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    marker_path().write_text("legacy\n", encoding="utf-8")
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
    monkeypatch.setattr("llm.local_setup_recovery.resume_pending_setup", resumed)
    monkeypatch.setattr("onboarding.local_download_wake.resume_pending_local_download", notified)
    monkeypatch.setattr("llm.local_setup_upgrade.done_path", lambda: SimpleNamespace(exists=lambda: True))
    monkeypatch.setattr(local_setup_upgrade, "start_upgrade", lambda: None)

    await app_startup_runtime.resume_local_setup()

    assert (await run_in_db(discovery.get_server, manual))["status"] == "online"


def test_legacy_service_migrates_only_after_ownership_proof(monkeypatch) -> None:
    from llm import local_setup_ownership as ownership

    commands: list[list[str]] = []
    monkeypatch.setattr(ownership, "legacy_setup_owns_current_omlx", lambda: True)
    monkeypatch.setattr(ownership.shutil, "which", lambda name: "/brew" if name == "brew" else None)
    monkeypatch.setattr(ownership, "run_command", lambda command: commands.append(command))

    assert ownership.retire_legacy_owned_omlx_service() is True
    assert commands == [["/brew", "services", "stop", ownership.OMLX_FORMULA]]

    monkeypatch.setattr(ownership, "legacy_setup_owns_current_omlx", lambda: False)
    assert ownership.retire_legacy_owned_omlx_service() is False
    assert commands == [["/brew", "services", "stop", ownership.OMLX_FORMULA]]


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
    from llm.local_setup_runtime import retire_legacy_setup_omlx_rows

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    lan = _add_through_settings(monkeypatch, "192.168.1.20")
    local = _add_through_settings(monkeypatch, "127.0.0.1")
    assert discovery.get_server(lan)["kind"] == "managed"
    assert discovery.get_server(lan)["source"] == "manual"
    _model_row("lan-model", f"omlx@{lan}/someone/model", "someone/model")
    _model_row("local-model", f"omlx@{local}/someone/local", "someone/local")

    assert retire_legacy_setup_omlx_rows() == []

    assert discovery.get_server(lan)["status"] == "online"
    assert catalog.get_model("lan-model")["id"] == "lan-model"
    assert catalog.is_valid_model_choice(f"omlx@{lan}/someone/model") is True
    assert discovery.get_server(local)["status"] == "online"
    assert catalog.is_valid_model_choice(f"omlx@{local}/someone/local") is True


def test_only_the_marker_gated_legacy_fingerprint_retires(monkeypatch, tmp_path) -> None:
    from llm.local_setup_managed import is_retired_local_omlx
    from llm.local_setup_ownership import marker_path
    from llm.local_setup_runtime import LOCAL_SERVER_NAME

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    row = {
        "provider": "omlx", "kind": "managed", "source": "manual",
        "instance_id": "127.0.0.1:8000", "instance_name": LOCAL_SERVER_NAME,
    }
    assert not is_retired_local_omlx(row)
    marker_path().write_text("legacy\n", encoding="utf-8")
    assert is_retired_local_omlx(row)
    assert not is_retired_local_omlx({**row, "instance_name": "Studio"})
    assert not is_retired_local_omlx({**row, "instance_id": "127.0.0.1:9999"})


@pytest.mark.parametrize("record", ["own-omlx-management.json", "own-omlx-use-mine"])
def test_use_mine_adopted_row_retires_on_its_own_port(monkeypatch, tmp_path, record) -> None:
    from llm.local_setup_managed import is_retired_local_omlx
    from llm.local_setup_runtime import LOCAL_SERVER_NAME

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    adopted = {
        "provider": "omlx", "kind": "managed", "source": "manual",
        "instance_id": "127.0.0.1:10240", "instance_name": LOCAL_SERVER_NAME,
    }
    hand_added = {**adopted, "instance_name": "My oMLX"}
    assert not is_retired_local_omlx(adopted)
    (tmp_path / record).write_text("{}", encoding="utf-8")
    assert is_retired_local_omlx(adopted)
    assert not is_retired_local_omlx(hand_added)
    assert not is_retired_local_omlx({**adopted, "instance_id": "192.168.1.20:10240"})


@pytest.mark.parametrize("row", [
    {"provider": "omlx", "kind": "managed", "instance_id": "192.168.1.20:8000"},
    {"provider": "omlx", "kind": "managed", "instance_id": "studio.local:8000"},
    {"provider": "omlx", "kind": "managed", "instance_id": "127.0.0.1:8000"},
    {"provider": "omlx", "kind": "external", "instance_id": "127.0.0.1:9000"},
    {"provider": "omlx", "kind": "managed", "instance_id": "corsia-managed"},
    {"provider": "ollama", "kind": "managed", "instance_id": "127.0.0.1:11434"},
])
def test_remote_external_and_own_rows_stay(row) -> None:
    from llm.local_setup_managed import is_retired_local_omlx

    assert not is_retired_local_omlx(row)


def test_only_state_root_row_is_corsia_managed() -> None:
    from infra.config import STATE_ROOT
    from llm.local_setup_managed import is_corsia_managed_server

    base = STATE_ROOT / "managed-omlx"
    row = {
        "provider": "omlx", "kind": "managed", "instance_id": "corsia-managed",
        "host": "127.0.0.1",
    }
    metadata = {
        "corsia_managed": True,
        "base_path": str(base),
        "model_dir": str(base / "models"),
    }

    assert is_corsia_managed_server(row, metadata)
    assert not is_corsia_managed_server({**row, "instance_id": "legacy"}, metadata)
    assert not is_corsia_managed_server(row, {**metadata, "base_path": "/tmp/foreign"})


# Process identity is produced by managed; the choice owner executes its exact set.

_PS = "  101  1 /opt/homebrew/opt/omlx/bin/omlx serve --port 8000\n"


def _fake_commands(monkeypatch, *, launchctl_list: str, record: list[list[str]]) -> None:
    from llm import local_setup_managed as managed

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
    monkeypatch.setattr("llm.local_setup_upgrade.pending_legacy_port", lambda: None)


def test_stop_mine_boots_out_a_launchd_job_for_this_session_only(monkeypatch) -> None:
    from llm import local_setup_foreign as foreign, local_setup_managed as managed

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
    from llm import local_setup_foreign as foreign, local_setup_managed as managed

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
    from llm import local_setup_foreign as foreign, local_setup_managed as managed

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
    monkeypatch.setattr("llm.local_setup_upgrade.pending_legacy_port", lambda: None)
    monkeypatch.setattr(foreign.os, "kill", lambda pid, _sig: signals.append(pid))

    processes = managed.foreign_omlx_processes()
    assert processes is not None
    assert foreign.stop_foreign_omlx_processes(processes) == "stopped"
    assert signals == [101]


def test_corsias_own_legacy_service_is_not_a_foreign_omlx(monkeypatch) -> None:
    from llm import local_setup_foreign as foreign, local_setup_managed as managed

    _fake_commands(monkeypatch, launchctl_list="PID\tStatus\tLabel\n101\t0\thomebrew.mxcl.omlx\n", record=[])
    monkeypatch.setattr("llm.local_setup_upgrade.pending_legacy_port", lambda: 8000)
    assert managed.foreign_omlx_processes() == ()
    assert foreign.apply_foreign_omlx_choice() is None


def test_legacy_ownership_runs_nothing_without_the_marker(monkeypatch, tmp_path) -> None:
    from llm import local_setup_ownership as ownership

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    monkeypatch.setattr(ownership, "installed_omlx", lambda: pytest.fail("must not run oMLX"))
    assert ownership.legacy_setup_owns_current_omlx() is False
