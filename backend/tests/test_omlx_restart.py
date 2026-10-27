"""What the supervisor does after its oMLX server exits unasked: restart, reload, stop, give up.

Everything here runs on every platform: the exit is handed to ``restart.on_exit`` directly. The
OS exit event that normally delivers it is in ``test_omlx_restart_exit_event.py`` (macOS only).
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from collections import deque

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm import local_keep_loaded  # noqa: E402
from llm.providers import omlx_process, omlx_restart as restart  # noqa: E402
from llm.providers import omlx_supervisor as sup  # noqa: E402

SID = "ns-restart-test"
GONE_PID = 111  # not a child of this process, so its exit status reads as unknown, never clean


@pytest.fixture(autouse=True)
def events(tmp_path, monkeypatch):
    """Isolate the runtime dir and capture the product events the restart emits."""
    from infra import config as cfg

    monkeypatch.setattr(cfg, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(sup, "RUNTIME_DIR", tmp_path)
    seen: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(restart, "emit_product_event", lambda cat, action, properties=None: seen.append((cat, action, properties)))
    yield seen
    sup.stop_watching()
    sup._life.clear()


def actions(seen) -> list[str]:
    return [action for _, action, _ in seen]


def sleeper() -> subprocess.Popen:
    return subprocess.Popen(
        ["sleep", "60"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


async def settle() -> None:
    """Let any restart that is going to happen run, without waiting on a timer for it."""
    for _ in range(20):
        await asyncio.sleep(0.02)


def crash(start, *, tail=lambda _sid: "", observed=("unknown (not our child)", False)) -> restart.Life:
    """Hand the supervisor an unexpected exit of this server."""
    life = sup._life.setdefault(SID, restart.Life())
    restart.on_exit(SID, life, GONE_PID, start, tail, observed)
    return life


def recording_start(ok: bool = True):
    calls: list[dict] = []

    async def start(cfg):
        calls.append(cfg)
        return {"ok": ok, "pid": 4242, "message": "boot failed"}

    return start, calls


def hanging_start(spawned: list[subprocess.Popen] | None = None):
    """A start that stays inside its awaits: before it spawns, or, with ``spawned``, after it did."""
    calls: list[dict] = []
    entered = asyncio.Event()

    async def start(cfg):
        calls.append(cfg)
        if spawned is not None:
            proc = sleeper()
            sup._write_pid(SID, proc.pid)
            spawned.append(proc)
        entered.set()
        await asyncio.sleep(60)
        return {"ok": True, "pid": 4242}

    return start, calls, entered


@pytest.fixture
def no_reload(monkeypatch):
    reloads: list[tuple[str, list[str]]] = []

    async def record(server_id, names):
        reloads.append((server_id, names))

    monkeypatch.setattr(local_keep_loaded, "reload_after_restart", record)
    return reloads


async def test_an_unexpected_exit_restarts_the_server_and_says_so(events, no_reload):
    start, calls = recording_start()
    crash(start)
    await settle()
    assert calls == [{"server_id": SID}]
    assert actions(events) == ["exit.unexpected", "restart.attempted", "restart.completed"]
    assert events[0][2]["status"] == "unknown (not our child)"


async def test_the_models_observed_loaded_at_the_exit_are_reloaded_in_sorted_order(monkeypatch, no_reload):
    import llm.network_discovery as discovery

    monkeypatch.setitem(discovery._LOADED_BY_SERVER, SID, {"zeta", "alpha", "mid"})
    start, _ = recording_start()
    crash(start)
    await settle()
    assert no_reload == [(SID, ["alpha", "mid", "zeta"])]


async def test_exit_forgets_loaded_models_before_restart_starts(monkeypatch, no_reload):
    from llm import network_discovery
    from llm.model_status import _network_status

    monkeypatch.setitem(network_discovery._LOADED_BY_SERVER, SID, {"model"})
    row = {"litellm_model": f"omlx@{SID}/model", "available": True}
    status = lambda: _network_status(row, {SID: "online"}, network_discovery.get_loaded_model_ids)
    assert status() == "ready"
    start, calls = recording_start()
    crash(start)
    assert status() != "ready"
    assert calls == []
    await settle()
    assert no_reload == [(SID, ["model"])]


async def test_probe_started_before_exit_cannot_restore_stale_readiness(monkeypatch, no_reload):
    from llm import network_discovery, server_observation

    monkeypatch.setitem(network_discovery._LOADED_BY_SERVER, SID, {"model"})
    generation = server_observation.observation_generation(SID)
    start, _ = recording_start()
    crash(start)
    await server_observation.record_reachable({"id": SID}, loaded={"model"}, generation=generation)
    assert network_discovery.get_loaded_model_ids(SID) == set()
    await settle()


async def test_load_started_before_exit_cannot_restore_stale_readiness(monkeypatch, no_reload):
    from llm import network_discovery, server_observation

    monkeypatch.setitem(network_discovery._LOADED_BY_SERVER, SID, {"model"})
    generation = server_observation.observation_generation(SID)
    start, _ = recording_start()
    crash(start)
    network_discovery.notify_cached_model_loaded(SID, "model", generation)
    assert network_discovery.get_loaded_model_ids(SID) == set()
    await settle()


@pytest.mark.parametrize("before_load", ["empty set", "no set yet"])
async def test_a_sweep_during_a_load_does_not_hide_the_loaded_model(monkeypatch, before_load):
    from infra.state import ws
    from llm import network_discovery, server_observation

    monkeypatch.setattr(network_discovery, "update_server_status", lambda *_a, **_k: None)
    monkeypatch.setattr(ws, "notify_models_changed", lambda: None)
    if before_load == "empty set":
        monkeypatch.setitem(network_discovery._LOADED_BY_SERVER, SID, set())
    else:
        monkeypatch.delitem(network_discovery._LOADED_BY_SERVER, SID, raising=False)
    at_load_start = server_observation.observation_generation(SID)
    sweep_start = server_observation.observation_generation(SID)
    await server_observation.record_reachable(
        {"id": SID}, loaded=set(), rediscover=False, generation=sweep_start,
    )
    network_discovery.notify_cached_model_loaded(SID, "model", at_load_start)
    assert network_discovery.get_loaded_model_ids(SID) == {"model"}


async def test_a_clean_exit_the_person_did_not_ask_for_is_logged_not_restarted(events, caplog, monkeypatch):
    start, calls = recording_start()
    with caplog.at_level(logging.INFO, logger=restart.log.name):
        crash(start, observed=("exit code 0", True))
        await settle()
    assert calls == [] and events == []
    assert "exited with code 0 without being asked; not restarting" in caplog.text


async def test_an_exit_after_an_announced_stop_is_not_a_crash(events):
    start, calls = recording_start()
    sup._life.setdefault(SID, restart.Life()).announce_stop()
    crash(start)
    await settle()
    assert calls == [] and events == []


async def test_a_stop_that_failed_leaves_a_later_crash_restartable(monkeypatch, no_reload):
    async def failing_stop(_cfg):
        return {"ok": False, "message": "oMLX failed to stop"}

    monkeypatch.setattr(sup, "_stop_server", failing_stop)
    assert (await sup.stop_server({"server_id": SID}))["ok"] is False
    start, calls = recording_start()
    crash(start)
    await settle()
    assert calls == [{"server_id": SID}]


async def test_a_stop_that_worked_forgets_the_server(events):
    proc = sleeper()
    sup._write_pid(SID, proc.pid)
    sup._life.setdefault(SID, restart.Life())
    result = await sup.stop_server({"server_id": SID})
    assert result["ok"] is True and SID not in sup._life and events == []
    proc.wait(timeout=5)


async def test_backend_shutdown_never_restarts_a_server():
    start, calls = recording_start()
    life = sup._life.setdefault(SID, restart.Life())
    sup.stop_watching()  # what the backend's shutdown path does before it tears anything down
    restart.on_exit(SID, life, GONE_PID, start, lambda _sid: "", ("unknown", False))
    await settle()
    assert calls == []


async def test_stop_server_with_no_server_id_says_so():
    assert (await sup.stop_server({}))["message"] == "stop_server: missing server_id in cfg"


async def test_a_stop_while_a_restart_is_starting_keeps_the_server_stopped():
    start, calls, entered = hanging_start()
    crash(start)
    await asyncio.wait_for(entered.wait(), timeout=5)

    result = await sup.stop_server({"server_id": SID})
    await settle()

    assert result["ok"] is True
    assert calls == [{"server_id": SID}]  # the restart never went on to a second attempt
    assert SID not in sup._life and not sup._pid_path(SID).exists()


async def test_a_stop_while_a_restart_has_spawned_stops_the_process():
    spawned: list[subprocess.Popen] = []
    start, _, entered = hanging_start(spawned)
    crash(start)
    await asyncio.wait_for(entered.wait(), timeout=5)

    result = await sup.stop_server({"server_id": SID})

    assert result["ok"] is True
    spawned[0].wait(timeout=5)  # raises if the process was left running
    assert not sup._pid_path(SID).exists()


async def test_quitting_while_a_restart_is_starting_leaves_no_omlx_process():
    spawned: list[subprocess.Popen] = []
    start, calls, entered = hanging_start(spawned)
    crash(start)
    await asyncio.wait_for(entered.wait(), timeout=5)

    results = await sup.stop_all_servers()  # no pid record could have found this one before

    assert [r["ok"] for r in results] == [True]
    spawned[0].wait(timeout=5)
    assert len(calls) == 1 and SID not in sup._life


async def test_quitting_before_a_restart_has_spawned_cancels_it():
    start, calls, entered = hanging_start()
    crash(start)
    await asyncio.wait_for(entered.wait(), timeout=5)

    results = await sup.stop_all_servers()
    await settle()

    assert [r["ok"] for r in results] == [True]
    assert len(calls) == 1 and SID not in sup._life and not sup._pid_path(SID).exists()


async def test_a_restart_that_raises_is_logged_and_does_not_block_a_later_one(caplog, no_reload, events):
    async def broken_start(cfg):
        raise RuntimeError("database is locked")

    with caplog.at_level(logging.ERROR, logger=restart.log.name):
        life = crash(broken_start)
        await settle()
    assert "restart stopped on an error" in caplog.text and "database is locked" in caplog.text
    assert life.restart is None
    failed = [p for _, action, p in events if action == "restart.failed"]
    assert failed == [{"provider": "omlx", "server_id": SID, "error": "RuntimeError", "message": "database is locked"}]
    start, calls = recording_start()
    crash(start)
    await settle()
    assert calls == [{"server_id": SID}]


async def test_restarts_stop_after_three_in_ten_minutes_and_the_event_and_log_say_why(events, caplog):
    start, calls = recording_start(ok=False)
    life = sup._life.setdefault(SID, restart.Life())
    with caplog.at_level(logging.INFO, logger=restart.log.name):
        await restart._restart(SID, life, "signal 9", [], start, lambda _sid: "last words")
        assert len(calls) == 3
        await restart._restart(SID, life, "signal 9", [], start, lambda _sid: "last words")  # window full
        assert len(calls) == 3
    assert actions(events).count("restart.attempted") == 3
    gave_up = [props for _, action, props in events if action == "restart.gave_up"]
    assert len(gave_up) == 2 and gave_up[0]["status"] == "signal 9" and gave_up[0]["restarts"] == 3
    assert "3 restarts in 10 minutes" in caplog.text and "last words" in caplog.text


async def test_restarts_older_than_ten_minutes_no_longer_count(no_reload):
    start, calls = recording_start()
    stale = time.monotonic() - restart.RESTART_WINDOW_S - 1
    life = sup._life.setdefault(SID, restart.Life())
    life.restarts = deque([stale, stale, stale])
    await restart._restart(SID, life, "exit code 1", [], start, lambda _sid: "")
    assert len(calls) == 1


async def test_a_person_starting_the_server_again_clears_the_bound_and_an_earlier_stop():
    life = sup._life.setdefault(SID, restart.Life())
    life.restarts = deque([time.monotonic()] * 3)
    life.stopping = True
    proc = sleeper()
    try:
        life.watch(proc.pid, lambda _status: None)
        assert not life.restarts and not life.stopping
    finally:
        life.drop_watch()
        proc.kill()
        proc.wait(timeout=5)


async def test_watching_does_nothing_where_the_os_has_no_exit_event(monkeypatch):
    class WithoutKqueue:
        """Stands in for ``select`` on Linux, which has no kqueue names."""

    monkeypatch.setattr(omlx_process, "select", WithoutKqueue())
    sup.watch(SID, 4242)
    assert sup._life[SID].unwatch is None


def reload_fakes(monkeypatch, events_log, *, sizes, loaded_now=(), load=None):
    """Stand in for oMLX and the host: the model rows, what is loaded, whether it fits, the load."""
    import llm.network_discovery as discovery
    from llm.providers import omlx_lifecycle, omlx_settings

    checked, loaded = [], []

    from infra.host_resources import GIB, load_verdict

    def verdict(size, others, room):
        checked.append(size / GIB if size is not None else None)
        return load_verdict(size, others, room)

    async def default_load(cfg, model):
        loaded.append(model)
        return {"ok": True}

    async def rows(_server_id):
        return [{"id": name, "estimated_size": size} for name, size in sizes.items()]

    async def now_loaded(_cfg):
        return [{"identifier": name, "size_bytes": sizes[name]} for name in [*loaded_now, *loaded]]

    async def refreshed(_sid):
        return {}

    async def never_pin(*_args):
        pytest.fail("a reload must not change a pin")

    monkeypatch.setattr(local_keep_loaded, "_model_rows", rows)
    monkeypatch.setattr(local_keep_loaded, "load_verdict", verdict)
    monkeypatch.setattr(local_keep_loaded, "model_room_bytes", lambda: 9 * GIB)
    monkeypatch.setattr(local_keep_loaded, "emit_product_event", lambda cat, action, properties=None: events_log.append((cat, action, properties)))
    monkeypatch.setattr(omlx_lifecycle, "load_model", load or default_load)
    monkeypatch.setattr(omlx_lifecycle, "list_loaded", now_loaded)
    monkeypatch.setattr(omlx_lifecycle, "read_loaded", now_loaded)
    monkeypatch.setattr(omlx_settings, "update_model_settings", never_pin)
    monkeypatch.setattr(discovery, "discover_and_notify", refreshed)
    monkeypatch.setattr(discovery, "notify_cached_model_loaded", lambda *_: None)
    local_keep_loaded._FAILED.clear()
    return checked, loaded


async def test_models_reload_in_order_each_through_the_fits_now_check_and_the_keep_loaded_events(monkeypatch):
    import infra.host_resources as host_resources

    gib = host_resources.GIB
    log: list = []
    checked, loaded = reload_fakes(
        monkeypatch, log,
        sizes={"first": 2 * gib, "too-big": 90 * gib, "pinned": gib, "last": gib},
        loaded_now=["pinned"],  # oMLX loaded its own pinned model before it answered health
    )

    await local_keep_loaded.reload_after_restart(SID, ["first", "too-big", "pinned", "last"])

    assert checked == [2.0, 90.0, 1.0]  # the pinned model is already loaded: oMLX reloaded it at start
    assert loaded == ["first", "last"]  # the model that did not fit stays unloaded, in order
    assert [(a, p["model_id"]) for _, a, p in log] == [("load.completed", "first"), ("load.completed", "last")]


async def test_a_model_that_fails_to_load_shows_on_its_row_and_emits_the_failure(monkeypatch):
    import infra.host_resources as host_resources

    async def load(*_):
        return {"ok": False, "message": "out of memory"}

    log: list = []
    reload_fakes(monkeypatch, log, sizes={"broken": host_resources.GIB}, load=load)
    await local_keep_loaded.reload_after_restart(SID, ["broken"])
    assert local_keep_loaded.failures(SID) == {"broken": local_keep_loaded.NOT_LOADED_UNPINNED}
    assert [(a, p["error"]) for _, a, p in log] == [("load.failed", "out of memory")]


async def test_a_pinned_model_that_fails_to_reload_shows_the_pinned_retry(monkeypatch):
    import infra.host_resources as host_resources

    async def load(*_):
        return {"ok": False, "message": "out of memory"}

    async def pinned_row(_server_id):
        return [{"id": "broken", "estimated_size": host_resources.GIB, "pinned": True}]

    reload_fakes(monkeypatch, [], sizes={"broken": host_resources.GIB}, load=load)
    monkeypatch.setattr(local_keep_loaded, "_model_rows", pinned_row)
    await local_keep_loaded.reload_after_restart(SID, ["broken"])
    assert local_keep_loaded.failures(SID) == {
        "broken": "Not loaded: oMLX couldn’t load it. Turn it off and on again to retry.",
    }


async def test_a_reload_that_cannot_read_oMLX_models_never_says_downloaded_but_not_kept(monkeypatch):
    async def unreadable(_server_id):
        return None

    reload_fakes(monkeypatch, [], sizes={})
    monkeypatch.setattr(local_keep_loaded, "_model_rows", unreadable)
    await local_keep_loaded.reload_after_restart(SID, ["gone"])
    assert local_keep_loaded.failures(SID) == {"gone": "Not loaded after oMLX restarted. Load it to try again."}

    from llm.local_model import LocalModel

    await local_keep_loaded.keep_loaded(SID, LocalModel(repository="fresh", download_size_bytes=1, context_window=0))
    assert local_keep_loaded.failures(SID)["fresh"] == local_keep_loaded.NOT_KEPT  # a download keeps its copy


async def test_an_empty_set_at_the_exit_reloads_nothing(monkeypatch):
    log: list = []
    checked, loaded = reload_fakes(monkeypatch, log, sizes={"x": 1})
    await local_keep_loaded.reload_after_restart(SID, [])
    assert loaded == [] and checked == [] and log == []
