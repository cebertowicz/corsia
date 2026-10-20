"""Recovery from a managed local oMLX start that fails after registration."""

from __future__ import annotations

import pytest

from test_local_setup_upgrade import (
    MODEL,
    _legacy_row_with_binding,
    _managed_dir,
    _tree,
    legacy,
    upgrade_env,
)


@pytest.mark.asyncio
async def test_a_managed_start_failure_restores_an_interrupted_move_and_reports_it(
        upgrade_env, isolated_db, legacy, monkeypatch) -> None:
    from infra.db_async import run_in_db
    from llm import catalog_network_models, local_setup, local_setup_upgrade, network_discovery
    from llm.local_setup import setup_status
    from llm.local_setup_managed import MANAGED_INSTANCE_ID
    from llm.local_setup_upgrade import move_recorded_models, upgrade_legacy_omlx
    from llm.resolution import resolve_execution_target

    old = await _legacy_row_with_binding()
    before = _tree(legacy / "models" / MODEL.repository)
    # A prior start completed the move but crashed before writing its done marker.
    assert [item.outcome for item in move_recorded_models(legacy / "models", [MODEL])] == ["moved"]
    assert not (legacy / "models" / MODEL.repository).exists()

    stopped: list[str] = []

    async def discover_fails(_server_id):
        upgrade_env.append("discover")
        raise RuntimeError("catalog refresh failed")

    async def stop_managed(cfg):
        stopped.append(cfg["server_id"])
        upgrade_env.append("stop managed")
        return {"ok": True}

    real_restore = local_setup_upgrade.restore_recorded_models

    def restore(*args):
        upgrade_env.append("restore models")
        return real_restore(*args)

    monkeypatch.setattr(catalog_network_models, "discover_network_models", discover_fails)
    monkeypatch.setattr("llm.providers.omlx_supervisor.stop_server", stop_managed)
    monkeypatch.setattr(local_setup_upgrade, "restore_recorded_models", restore)

    assert await upgrade_legacy_omlx() is None

    assert _tree(legacy / "models" / MODEL.repository) == before
    assert not (_managed_dir() / MODEL.repository).exists()
    assert upgrade_env == [
        "stop service", "start managed", "discover", "stop managed", "restore models", "restart service",
    ]
    assert len(stopped) == 1
    assert (await run_in_db(network_discovery.get_server, stopped[0]))["instance_id"] == MANAGED_INSTANCE_ID
    assert (await run_in_db(network_discovery.get_server, old))["status"] != "removed"
    target = await run_in_db(resolve_execution_target, "legacy-qwen")
    assert (target.available, target.transport_model) == (True, f"omlx@{old}/{MODEL.model}")
    status = setup_status()
    assert status["state"] == "setup_failed"
    assert status["cause"] == "other"
    assert status["ref"].startswith("cli-")
    assert "running again" not in local_setup._flight.result["message"].lower()
