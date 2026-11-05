"""A running local model server moves onto the oMLX build the app ships, once idle and never otherwise."""

from __future__ import annotations

from llm.omlx_release import OMLX_VERSION
from llm.providers import omlx_restart, omlx_supervisor
from llm.providers import omlx_upgrade


def _seat(monkeypatch, *, recorded: str | None, quiet: bool = True, start_ok: bool = True):
    calls: list[object] = []
    seen: dict[str, object] = {}

    async def fake_state(server_id, *, reads):
        return ("idle", reads)

    async def fake_wait(read, *, timeout, interval):
        seen["state"] = await read()
        seen["timeout"], seen["interval"] = timeout, interval
        return quiet

    async def read_loaded(_cfg):
        calls.append(("loaded", None))
        return [{"identifier": "B/second-mtp"}, {"identifier": "A/first-oQ4e-mtp"}]

    async def stop_server(cfg):
        calls.append(("stop", cfg["server_id"]))
        return {"ok": True}

    async def start_server(cfg):
        calls.append(("start", cfg["server_id"]))
        return {"ok": start_ok, "message": None if start_ok else "boot failed"}

    async def reload_after_restart(server_id, model_ids):
        calls.append(("reload", server_id, list(model_ids)))

    monkeypatch.setattr("llm.local_sources.corsia_omlx_server", lambda: {"id": "seat", "status": "online"})
    monkeypatch.setattr("llm.providers.omlx_process.recorded_omlx_version", lambda runtime_dir, server_id: recorded)
    monkeypatch.setattr(omlx_supervisor, "_life", {"seat": omlx_restart.Life()})
    monkeypatch.setattr("infra.carry_in_capacity.omlx_state", fake_state)
    monkeypatch.setattr("infra.carry_in_capacity.wait_until_quiet", fake_wait)
    monkeypatch.setattr("llm.providers.omlx_lifecycle.read_loaded", read_loaded)
    monkeypatch.setattr("llm.providers.omlx_supervisor._stop_server", stop_server)
    monkeypatch.setattr("llm.providers.omlx_supervisor.start_server", start_server)
    monkeypatch.setattr("llm.local_keep_loaded.reload_after_restart", reload_after_restart)
    return calls, seen


async def test_the_bundled_build_is_left_alone(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded=OMLX_VERSION)

    assert await omlx_upgrade.upgrade_managed_omlx() is None
    assert calls == []


async def test_an_outdated_seat_moves_once_idle_and_reloads_the_same_models(isolated_db, monkeypatch):
    calls, seen = _seat(monkeypatch, recorded="0.7.1rc1")

    result = await omlx_upgrade.upgrade_managed_omlx()

    assert result == {"restarted": True, "loaded": ["A/first-oQ4e-mtp", "B/second-mtp"]}
    assert calls == [
        ("loaded", None),
        ("stop", "seat"),
        ("start", "seat"),
        ("reload", "seat", ["A/first-oQ4e-mtp", "B/second-mtp"]),
        ("loaded", None),
    ]
    assert seen["state"] == ("idle", 2)  # idle needs both counts, proved twice
    assert (seen["timeout"], seen["interval"]) == (float("inf"), omlx_upgrade.QUIET_INTERVAL_S)


async def test_a_record_from_before_the_field_exists_counts_as_outdated(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded=None)

    assert (await omlx_upgrade.upgrade_managed_omlx())["restarted"] is True
    assert [call[0] for call in calls] == ["loaded", "stop", "start", "reload", "loaded"]


async def test_a_busy_seat_stays_on_its_build(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="0.7.1rc1", quiet=False)

    assert await omlx_upgrade.upgrade_managed_omlx() == {"restarted": False, "reason": "not_idle"}
    assert calls == []


async def test_a_failed_start_loads_nothing(isolated_db, monkeypatch):
    calls, _ = _seat(monkeypatch, recorded="0.7.1rc1", start_ok=False)

    assert await omlx_upgrade.upgrade_managed_omlx() == {"restarted": False, "reason": "start_failed"}
    assert [call[0] for call in calls] == ["loaded", "stop", "start"]
