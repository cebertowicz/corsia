"""T7: after the state folder moves, nothing keeps reading or writing the old one."""

from __future__ import annotations

import asyncio
import json

import pytest


def _register_moved_install(tmp_path, monkeypatch, *, cache: dict | None = None):
    from infra.managed_process_store import upsert_transition
    from llm import network_discovery as discovery
    from llm.managed_omlx_binary import MANAGED_INSTANCE_ID

    old_root = tmp_path / "older-root" / "state"
    new_root = tmp_path / "CORSIA" / "state"
    old_base = old_root / "managed-omlx"
    new_base = new_root / "managed-omlx"
    (new_base / "models").mkdir(parents=True)
    (new_root / "runtime" / "managed-processes").mkdir(parents=True)
    monkeypatch.setattr("infra.config.STATE_ROOT", new_root)
    monkeypatch.setattr("infra.config.RUNTIME_DIR", new_root / "runtime")
    # The moved oMLX folder still names the old place everywhere it stores a path.
    (new_base / "settings.json").write_text(json.dumps({
        "base_path": str(old_base),
        "server": {"port": 8210},
        "model": {"model_dir": str(old_base / "models"), "model_dirs": [str(old_base / "models")]},
        "cache": cache if cache is not None else {"ssd_cache_dir": str(old_base / "cache")},
    }))
    server_id = discovery.upsert_server(
        provider="omlx", instance_id=MANAGED_INSTANCE_ID, instance_name="Local",
        host="127.0.0.1", port=8210, status="offline", kind="managed", source="corsia",
    )
    # A row written before setup flagged it: identity is the row's own columns.
    discovery.set_server_metadata(
        server_id, base_path=str(old_base), model_dir=str(old_base / "models"),
        paged_ssd_cache_dir=str(old_base / "cache"),
    )
    row = {
        "handle": "h-live", "thread_id": "t1", "turn_id": "turn-1", "long_running": 1,
        "pid": 123, "pgid": 123, "creation_fingerprint": "fp",
        "log_path": str(old_root / "runtime" / "managed-processes" / "h-live.log"),
        "state": "running", "started_at": "2026-10-25T00:00:00Z", "log_truncated": 0,
    }
    upsert_transition(row)
    upsert_transition({**row, "handle": "h-done", "state": "exited",
                       "log_path": str(old_root / "runtime" / "managed-processes" / "h-done.log")})
    return server_id, old_root, new_root


def _launch_argv(server_id: str, monkeypatch) -> tuple[dict, list[str]]:
    from llm.providers import omlx_supervisor as sup

    argv: list[str] = []

    class Spawned:
        pid = 1234

    async def healthy(*_args, **_kwargs):
        return "healthy"

    def spawn(observed_argv, _log_path):
        argv.extend(observed_argv)
        return Spawned()

    monkeypatch.setattr(sup, "_port_owner_pid", lambda *_args: None)
    monkeypatch.setattr(sup.process, "managed_omlx_install_error", lambda: None)
    monkeypatch.setattr(sup.process, "managed_omlx_executable", lambda: "/bundled/omlx")
    monkeypatch.setattr(sup, "_spawn_process", spawn)
    monkeypatch.setattr(sup, "_wait_for_healthy", healthy)
    monkeypatch.setattr(sup, "_mark_endpoint_alive", lambda *_args: None)
    result = asyncio.run(sup.start_server({"server_id": server_id, "host": "127.0.0.1", "port": 8210}))
    return result, argv


def _flag(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


def test_a_moved_managed_omlx_launches_only_on_current_folders(isolated_db, tmp_path, monkeypatch) -> None:
    server_id, old_root, new_root = _register_moved_install(tmp_path, monkeypatch)
    new_base = new_root / "managed-omlx"

    result, argv = _launch_argv(server_id, monkeypatch)

    assert result["ok"] is True
    assert argv[0] == "/bundled/omlx"
    assert _flag(argv, "--base-path") == str(new_base)
    assert _flag(argv, "--model-dir") == str(new_base / "models")
    assert _flag(argv, "--paged-ssd-cache-dir") == str(new_base / "cache")
    assert not any(str(old_root) in arg for arg in argv)


def test_a_cache_that_omlx_settings_turn_off_stays_off(isolated_db, tmp_path, monkeypatch) -> None:
    server_id, old_root, _new_root = _register_moved_install(tmp_path, monkeypatch, cache={"enabled": False})

    result, argv = _launch_argv(server_id, monkeypatch)

    assert result["ok"] is True
    assert "--paged-ssd-cache-dir" not in argv
    assert not any(str(old_root) in arg for arg in argv)


def test_a_person_s_own_omlx_keeps_its_own_folders(isolated_db, tmp_path, monkeypatch) -> None:
    from llm import network_discovery as discovery

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path / "CORSIA" / "state")
    server_id = discovery.upsert_server(
        provider="omlx", instance_id="theirs", instance_name="Mine",
        host="127.0.0.1", port=8000, status="offline", kind="network", source="discovered",
    )
    discovery.set_server_metadata(server_id, base_path="/Users/someone/.omlx", model_dir="/Volumes/models")
    monkeypatch.setattr("llm.providers.omlx_supervisor.shutil.which", lambda _name: "/opt/homebrew/bin/omlx")

    result, argv = _launch_argv(server_id, monkeypatch)

    assert result["ok"] is True
    assert argv[0] == "/opt/homebrew/bin/omlx"
    assert _flag(argv, "--base-path") == "/Users/someone/.omlx"
    assert _flag(argv, "--model-dir") == "/Volumes/models"


def test_heal_repoints_live_handles_only(isolated_db, tmp_path, monkeypatch) -> None:
    from infra.database import get_db
    from infra.moved_state_heal import heal_moved_state_paths

    _server_id, old_root, new_root = _register_moved_install(tmp_path, monkeypatch)

    heal_moved_state_paths()

    logs = dict(get_db().execute("SELECT handle, log_path FROM managed_process_handles").fetchall())
    assert logs["h-live"] == str(new_root / "runtime" / "managed-processes" / "h-live.log")
    assert logs["h-done"] == str(old_root / "runtime" / "managed-processes" / "h-done.log")
    assert not old_root.exists()


@pytest.mark.parametrize("state", ["interrupted_by_restart", "exited"])
def test_heal_leaves_ended_handles_as_history(isolated_db, tmp_path, monkeypatch, state) -> None:
    from infra.database import get_db
    from infra.managed_process_store import upsert_transition
    from infra.moved_state_heal import heal_moved_state_paths

    monkeypatch.setattr("infra.config.RUNTIME_DIR", tmp_path / "runtime")
    upsert_transition({
        "handle": "h", "thread_id": "t", "turn_id": "u", "long_running": 0, "pid": 1, "pgid": 1,
        "creation_fingerprint": "fp", "log_path": "/old/runtime/managed-processes/h.log",
        "state": state, "started_at": "2026-10-25T00:00:00Z", "log_truncated": 0,
    })

    heal_moved_state_paths()

    assert get_db().execute("SELECT log_path FROM managed_process_handles").fetchone()[0] == (
        "/old/runtime/managed-processes/h.log"
    )
