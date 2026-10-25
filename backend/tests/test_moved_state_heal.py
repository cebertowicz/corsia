"""T7: after the state folder moves, startup repoints the stored state paths."""

from __future__ import annotations

import json

import pytest


def _register_moved_install(tmp_path, monkeypatch):
    from infra.managed_process_store import upsert_transition
    from llm import network_discovery as discovery
    from llm.managed_omlx_binary import MANAGED_INSTANCE_ID

    old_root = tmp_path / "com.cebertowicz.corsia"
    new_root = tmp_path / "CORSIA" / "state"
    old_base = old_root / "managed-omlx"
    new_base = new_root / "managed-omlx"
    (new_base / "models").mkdir(parents=True)
    (new_root / "runtime" / "managed-processes").mkdir(parents=True)
    monkeypatch.setattr("infra.config.STATE_ROOT", new_root)
    monkeypatch.setattr("infra.config.RUNTIME_DIR", new_root / "runtime")
    (new_base / "settings.json").write_text(json.dumps({
        "server": {"port": 8210},
        "model": {"model_dir": str(old_base / "models")},
        "cache": {"paged_ssd_cache_dir": str(old_base / "cache"), "label": "com.cebertowicz.corsia-ish"},
    }))
    (new_base / "extra.yaml").write_text(f"log_dir: {old_base}/logs\nother: {old_base}x\n")
    server_id = discovery.upsert_server(
        provider="omlx", instance_id=MANAGED_INSTANCE_ID, instance_name="Local",
        host="127.0.0.1", port=8210, status="offline", kind="managed", source="corsia",
    )
    discovery.set_server_metadata(
        server_id, base_path=str(old_base), model_dir=str(old_base / "models"),
        paged_ssd_cache_dir=str(old_base / "cache"), corsia_managed=True,
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


def test_heal_repoints_managed_omlx_and_live_handles(isolated_db, tmp_path, monkeypatch) -> None:
    from infra.database import get_db
    from infra.moved_state_heal import heal_moved_state_paths
    from llm import network_discovery as discovery
    from llm.providers import omlx_process
    from llm.providers.omlx_supervisor import _is_managed_omlx_row

    server_id, old_root, new_root = _register_moved_install(tmp_path, monkeypatch)
    new_base = new_root / "managed-omlx"

    heal_moved_state_paths()

    metadata = discovery.get_server_metadata(server_id)
    assert metadata["base_path"] == str(new_base)
    assert metadata["model_dir"] == str(new_base / "models")
    assert metadata["paged_ssd_cache_dir"] == str(new_base / "cache")
    assert discovery.get_server(server_id)["id"] == server_id
    assert _is_managed_omlx_row(metadata)
    argv = omlx_process.build_argv("127.0.0.1", 8210, metadata, executable="/bundle/omlx")
    assert argv[argv.index("--base-path") + 1] == str(new_base)
    settings = json.loads((new_base / "settings.json").read_text())
    assert settings["model"]["model_dir"] == str(new_base / "models")
    assert settings["cache"] == {"paged_ssd_cache_dir": str(new_base / "cache"),
                                 "label": "com.cebertowicz.corsia-ish"}
    assert (new_base / "extra.yaml").read_text() == (
        f"log_dir: {new_base}/logs\nother: {old_root / 'managed-omlx'}x\n"
    )
    logs = dict(get_db().execute("SELECT handle, log_path FROM managed_process_handles").fetchall())
    assert logs["h-live"] == str(new_root / "runtime" / "managed-processes" / "h-live.log")
    assert logs["h-done"] == str(old_root / "runtime" / "managed-processes" / "h-done.log")
    assert not old_root.exists()


def test_heal_is_a_no_op_when_paths_agree(isolated_db, tmp_path, monkeypatch) -> None:
    from infra.moved_state_heal import heal_moved_state_paths
    from llm import network_discovery as discovery

    server_id, _old, new_root = _register_moved_install(tmp_path, monkeypatch)
    heal_moved_state_paths()
    settings = (new_root / "managed-omlx" / "settings.json").stat().st_mtime_ns
    before = discovery.get_server(server_id)["updated_at"]

    heal_moved_state_paths()

    assert (new_root / "managed-omlx" / "settings.json").stat().st_mtime_ns == settings
    assert discovery.get_server(server_id)["updated_at"] == before


def test_moved_row_without_matching_base_still_counts_as_managed() -> None:
    from llm.providers.omlx_supervisor import _is_managed_omlx_row

    assert _is_managed_omlx_row({"base_path": "/elsewhere/managed-omlx", "corsia_managed": True})
    assert not _is_managed_omlx_row({"base_path": "/elsewhere/managed-omlx"})


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
