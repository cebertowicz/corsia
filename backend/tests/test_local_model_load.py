"""A send loads a not-loaded local model only when it fits now, never by unloading another.

``readiness`` is the cheap read admission makes; ``ensure_loaded`` is the one load a person's
send may start. Background work never loads: it asks oMLX whether the model is loaded.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from infra.host_resources import GIB, load_verdict
from llm import local_model_load, network_discovery
from llm.local_model_load import ModelLoadFailed, Readiness, ensure_loaded, readiness, seat_lock
from llm.resolution import ExecutionTarget

SERVER = "ns-seat"
SERVER_KEY = (SERVER, "target")


def _target(model: str = "target", *, provider: str = "omlx", harness: str = "native") -> ExecutionTarget:
    return ExecutionTarget(
        catalog_id=f"{provider}/{model}", harness=harness, provider=provider, backend="network",
        transport_model=f"omlx@{SERVER}/{model}" if provider == "omlx" else model,
        expected_reported_model=None, context_window=32_768, available=True, availability_reason=None,
        display_name=model, supports_reasoning=False,
    )


class Seat:
    """One oMLX seat on a disposable database: its status, models, loaded set and free room."""

    def __init__(self, monkeypatch, tmp_path) -> None:
        self.path = tmp_path / "seat.db"
        self.loaded: set[str] = set()
        self.room_gib = 100.0
        self.loads: list[str] = []
        self.on_this_mac = True
        db = self.connect()
        db.executescript(
            "CREATE TABLE network_servers (id TEXT PRIMARY KEY, status TEXT, kind TEXT, port INTEGER, metadata_json TEXT);"
            "CREATE TABLE network_server_endpoints (server_id TEXT, host TEXT);"
            "CREATE TABLE models (litellm_model TEXT PRIMARY KEY, metadata_json TEXT);"
        )
        db.execute("INSERT INTO network_servers (id, status, kind) VALUES (?, 'online', 'omlx')", (SERVER,))
        db.execute("INSERT INTO network_server_endpoints VALUES (?, '127.0.0.1')", (SERVER,))
        db.commit()
        db.close()
        monkeypatch.setattr(network_discovery, "_LOADED_BY_SERVER", {SERVER: self.loaded})
        monkeypatch.setattr(local_model_load, "model_room_bytes", lambda: int(self.room_gib * GIB))
        monkeypatch.setattr("infra.database.get_db", self.connect)
        # Locality is llm.locality's to decide; this checks the row and endpoints readiness hands it.
        monkeypatch.setattr("llm.locality.server_on_this_mac", self._locality)

    def _locality(self, server: dict, endpoints: list[dict]) -> bool:
        assert server["id"] == SERVER and [e["host"] for e in endpoints] == ["127.0.0.1"]
        return self.on_this_mac

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def model(self, name: str, gib: float | None, *, loaded: bool = False) -> None:
        metadata = json.dumps({"size_bytes": int(gib * GIB)} if gib else {})
        db = self.connect()
        db.execute("INSERT INTO models VALUES (?, ?)", (f"omlx@{SERVER}/{name}", metadata))
        db.commit()
        db.close()
        if loaded:
            self.loaded.add(name)

    def status(self, status: str) -> None:
        db = self.connect()
        db.execute("UPDATE network_servers SET status=?", (status,))
        db.commit()
        db.close()

    def readiness(self, name: str = "target") -> Readiness:
        db = self.connect()
        try:
            return readiness(_target(name), db=db)
        finally:
            db.close()


@pytest.fixture
def seat(monkeypatch, tmp_path) -> Seat:
    monkeypatch.setattr(local_model_load, "_SEAT_LOCKS", {})
    monkeypatch.setattr(local_model_load, "_LOADS", {})
    return Seat(monkeypatch, tmp_path)


@pytest.fixture
def lifecycle(monkeypatch, seat):
    """oMLX's explicit load, recorded; any unload is a failure of the test, and a loaded model joins the cache."""
    state = {"result": {"ok": True}, "up": True, "gate": None, "entered": asyncio.Event()}

    async def load_model(cfg, model, **_opts):
        assert cfg == {"server_id": SERVER}
        seat.loads.append(model)
        state["entered"].set()
        if state["gate"] is not None:
            await state["gate"].wait()
        return state["result"]

    async def check_server(_cfg):
        return {"running": state["up"]}

    async def read_loaded(_cfg):
        if not state["up"]:
            return None
        return [{"identifier": name, "size_bytes": 5 * GIB} for name in seat.loaded]

    def never_unload(*_a, **_k):
        raise AssertionError("a send must never unload a model")

    def notify(server_id, model_id):
        network_discovery._LOADED_BY_SERVER[server_id].add(model_id)

    monkeypatch.setattr("llm.providers.omlx_lifecycle.load_model", load_model)
    monkeypatch.setattr("llm.providers.omlx_lifecycle.read_loaded", read_loaded)
    monkeypatch.setattr("llm.providers.omlx_lifecycle.check_server", check_server)
    monkeypatch.setattr("llm.providers.omlx_lifecycle.unload_model", never_unload)
    monkeypatch.setattr("llm.lifecycle.unload_model", never_unload)
    monkeypatch.setattr("llm.providers.omlx.unload_model", never_unload)
    monkeypatch.setattr("llm.managed_engine.OmlxEngine.unload", never_unload)
    monkeypatch.setattr(network_discovery, "notify_cached_model_loaded", notify)
    return state


# readiness


def test_a_target_that_is_not_a_local_omlx_model_is_outside_scope(seat) -> None:
    for target in (_target("sonnet", provider="anthropic", harness="claude-code"),
                   _target("remote", provider="external")):
        db = seat.connect()
        assert readiness(target, db=db) is None
        db.close()


def test_a_loaded_model_counts_the_other_models_loaded_on_its_seat(seat) -> None:
    seat.model("target", 10, loaded=True)
    seat.model("a", 5, loaded=True)
    seat.model("b", 5, loaded=True)
    assert seat.readiness() == Readiness("loaded", 2)


def test_a_model_that_fits_the_room_now_fits_now(seat) -> None:
    seat.model("target", 10)
    seat.model("a", 5, loaded=True)
    seat.room_gib = 10  # exactly the room: it fits
    assert seat.readiness() == Readiness("fits_now", 1)


def test_2a_one_model_loaded_and_unloading_it_would_make_room(seat) -> None:
    seat.model("target", 10)
    seat.model("a", 8, loaded=True)
    seat.room_gib = 5
    assert seat.readiness() == Readiness("no_room", 1)


def test_2b_several_models_loaded_and_unloading_them_would_make_room(seat) -> None:
    seat.model("target", 12)
    seat.model("a", 4, loaded=True)
    seat.model("b", 4, loaded=True)
    seat.room_gib = 5
    assert seat.readiness() == Readiness("no_room", 2)


def test_2c_nothing_else_loaded_so_only_other_apps_hold_the_memory(seat) -> None:
    seat.model("target", 10)
    seat.room_gib = 5
    assert seat.readiness() == Readiness("no_memory", 0)


def test_2d_unloading_every_other_model_still_wouldnt_be_enough(seat) -> None:
    seat.model("target", 30)
    seat.model("a", 4, loaded=True)
    seat.model("b", 5, loaded=True)
    seat.room_gib = 1
    assert seat.readiness() == Readiness("no_memory", 2)


def test_a_loaded_model_with_no_reported_size_cant_be_judged_so_it_reads_no_room(seat) -> None:
    seat.model("target", 30)
    seat.model("a", None, loaded=True)
    seat.room_gib = 1
    assert seat.readiness() == Readiness("no_room", 1)


def test_a_remote_seat_with_another_model_loaded_has_unknown_fit(seat) -> None:
    seat.model("target", 30)
    seat.model("a", 8, loaded=True)
    seat.room_gib = 1
    seat.on_this_mac = False
    assert seat.readiness() == Readiness("no_room", 1)


def test_a_model_omlx_reported_no_size_for_counts_as_fitting(seat) -> None:
    seat.model("target", None)
    seat.room_gib = 0
    assert seat.readiness() == Readiness("fits_now", 0)


@pytest.mark.parametrize("status", ["offline", "disabled"])
def test_a_server_that_isnt_online_is_unreachable(seat, status) -> None:
    seat.model("target", 10, loaded=True)
    seat.status(status)
    assert seat.readiness() == Readiness("unreachable", 0)


def test_an_unknown_server_is_unreachable(seat) -> None:
    db = seat.connect()
    other = replace(_target(), transport_model="omlx@ns-gone/target")
    assert readiness(other, db=db) == Readiness("unreachable", 0)
    db.close()


def test_readiness_is_synchronous_and_never_loads_or_touches_the_network(seat, lifecycle, monkeypatch) -> None:
    import httpx

    def no_network(*_a, **_k):
        raise AssertionError("readiness must not make a network call")

    monkeypatch.setattr(httpx.Client, "send", no_network)
    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
    seat.model("target", 10)
    seat.model("a", 8, loaded=True)
    seat.room_gib = 3
    assert not inspect.iscoroutinefunction(readiness)
    assert seat.readiness().state == "no_room" and seat.loads == []


@pytest.mark.parametrize(("size", "others", "room", "verdict"), [
    (10, [], 10, "fits_now"),
    (10, [], 9, "no_memory"),
    (10, [8], 3, "no_room"),
    (10, [8], 1, "no_memory"),
    (10, [None], 1, "no_room"),
    (None, [8], 0, "no_room"),
])
def test_load_verdict_is_judged_in_host_resources(size, others, room, verdict) -> None:
    gib = lambda value: None if value is None else int(value * GIB)  # noqa: E731
    assert load_verdict(gib(size), [gib(o) for o in others], gib(room)) == verdict


# ensure_loaded


@pytest.mark.asyncio
async def test_a_loaded_model_returns_at_once_without_a_load(seat, lifecycle) -> None:
    seat.model("target", 10, loaded=True)
    await ensure_loaded(_target())
    assert seat.loads == []


@pytest.mark.asyncio
async def test_a_non_omlx_target_returns_at_once(seat, lifecycle) -> None:
    await ensure_loaded(_target("sonnet", provider="anthropic", harness="claude-code"))
    assert seat.loads == []


@pytest.mark.asyncio
async def test_a_model_that_fits_is_loaded_through_omlxs_explicit_load(seat, lifecycle) -> None:
    seat.model("target", 10)
    await ensure_loaded(_target())
    assert seat.loads == ["target"]
    assert seat.readiness().state == "loaded"


@pytest.mark.asyncio
@pytest.mark.parametrize(("size", "others", "room", "cause"), [
    (10, [8], 5, "no_room"), (10, [], 5, "no_memory"), (30, [4, 5], 1, "no_memory"),
])
async def test_a_model_that_doesnt_fit_is_refused_and_nothing_is_unloaded(
    seat, lifecycle, size, others, room, cause,
) -> None:
    seat.model("target", size)
    for index, other in enumerate(others):
        seat.model(f"other{index}", other, loaded=True)
    seat.room_gib = room
    with pytest.raises(ModelLoadFailed) as failure:
        await ensure_loaded(_target())
    assert failure.value.cause == cause
    # The explicit load wasn't called, so oMLX had no chance to evict; every unload is patched to fail.
    assert seat.loads == []
    assert seat.loaded == {f"other{index}" for index in range(len(others))}


@pytest.mark.asyncio
async def test_a_server_that_isnt_online_fails_unreachable_without_a_load(seat, lifecycle) -> None:
    seat.model("target", 10)
    seat.status("offline")
    with pytest.raises(ModelLoadFailed) as failure:
        await ensure_loaded(_target())
    assert failure.value.cause == "unreachable" and seat.loads == []


@pytest.mark.asyncio
async def test_the_fit_is_rechecked_under_the_lock(seat, lifecycle) -> None:
    seat.model("target", 10)
    await seat_lock(SERVER).acquire()  # another load holds the seat
    send = asyncio.create_task(ensure_loaded(_target()))
    while SERVER_KEY not in local_model_load._LOADS:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0)
    seat.room_gib = 1  # it fit when the send was admitted; the other load took the memory
    seat_lock(SERVER).release()
    with pytest.raises(ModelLoadFailed) as failure:
        await send
    assert failure.value.cause == "no_memory" and seat.loads == []


@pytest.mark.asyncio
async def test_loads_on_one_seat_run_one_at_a_time_and_the_second_sees_the_memory_the_first_took(
    seat, lifecycle,
) -> None:
    seat.room_gib = 12
    seat.model("first", 10)
    seat.model("second", 10)
    lifecycle["gate"] = asyncio.Event()
    first = asyncio.create_task(ensure_loaded(_target("first")))
    await lifecycle["entered"].wait()
    second = asyncio.create_task(ensure_loaded(_target("second")))
    await asyncio.sleep(0.05)
    assert seat.loads == ["first"]  # the second waits for the seat
    seat.room_gib = 2  # the first load took its memory
    lifecycle["gate"].set()
    await first
    with pytest.raises(ModelLoadFailed) as failure:
        await second
    assert failure.value.cause == "no_room" and seat.loads == ["first"]  # never unloaded the first to make room


@pytest.mark.asyncio
async def test_a_caller_cancelled_during_the_load_leaves_it_running_to_completion(seat, lifecycle) -> None:
    seat.model("target", 10)
    lifecycle["gate"] = asyncio.Event()
    send = asyncio.create_task(ensure_loaded(_target()))
    await lifecycle["entered"].wait()
    load = local_model_load._LOADS[SERVER_KEY]
    send.cancel()
    with pytest.raises(asyncio.CancelledError):
        await send
    assert not load.done() and not load.cancelled()  # Stop neither cancelled the load nor waited on it
    lifecycle["gate"].set()
    await load
    assert seat.readiness().state == "loaded" and seat.loads == ["target"]
    assert SERVER_KEY not in local_model_load._LOADS


@pytest.mark.asyncio
async def test_two_sends_for_one_model_share_one_load(seat, lifecycle) -> None:
    seat.model("target", 10)
    lifecycle["gate"] = asyncio.Event()
    sends = [asyncio.create_task(ensure_loaded(_target())) for _ in range(2)]
    await lifecycle["entered"].wait()
    await asyncio.sleep(0.05)
    lifecycle["gate"].set()
    await asyncio.gather(*sends)
    assert seat.loads == ["target"]


@pytest.mark.asyncio
async def test_a_failed_shared_load_fails_every_waiting_send(seat, lifecycle) -> None:
    seat.model("target", 10)
    lifecycle["gate"] = asyncio.Event()
    lifecycle["result"] = {"ok": False, "message": "oMLX refused"}
    sends = [asyncio.create_task(ensure_loaded(_target())) for _ in range(2)]
    await lifecycle["entered"].wait()
    await asyncio.sleep(0.05)
    lifecycle["gate"].set()
    results = await asyncio.gather(*sends, return_exceptions=True)
    failures = [r for r in results if isinstance(r, ModelLoadFailed)]
    assert [f.cause for f in failures] == ["load_failed", "load_failed"] and seat.loads == ["target"]


@pytest.mark.asyncio
async def test_a_finished_download_and_a_send_load_one_at_a_time_on_one_seat(seat, lifecycle, monkeypatch) -> None:
    from llm import local_keep_loaded
    from llm.local_model import LocalModel

    async def model_rows(_server_id):
        return [{"id": "second", "estimated_size": 10 * GIB, "pinned": False}]

    async def pin(_cfg, _model, _body):
        return {"ok": True, "settings": {"pinned": True}}

    async def discover(*_a):
        return None

    monkeypatch.setattr(local_keep_loaded, "_model_rows", model_rows)
    monkeypatch.setattr(local_keep_loaded, "pin_room_bytes", lambda: 100 * GIB)
    monkeypatch.setattr(local_keep_loaded, "model_room_bytes", lambda: 100 * GIB)
    monkeypatch.setattr(local_keep_loaded, "emit_product_event", lambda *_a, **_k: True)
    monkeypatch.setattr("llm.providers.omlx_settings.update_model_settings", pin)
    monkeypatch.setattr(network_discovery, "discover_and_notify", discover)
    seat.model("target", 10)
    lifecycle["gate"] = asyncio.Event()
    send = asyncio.create_task(ensure_loaded(_target()))
    await lifecycle["entered"].wait()
    download = asyncio.create_task(local_keep_loaded.keep_loaded(SERVER, LocalModel("Jundot/second", 1_000_000, 32_768)))
    await asyncio.sleep(0.05)
    assert seat.loads == ["target"]  # the download's check and load wait for the send's load
    lifecycle["gate"].set()
    await asyncio.gather(send, download)
    assert seat.loads == ["target", "second"]  # each loaded once, one after the other


@pytest.mark.asyncio
async def test_an_unexpected_failure_nobody_was_left_waiting_for_is_logged(seat, lifecycle, monkeypatch, caplog) -> None:
    seat.model("target", 10)
    lifecycle["gate"] = asyncio.Event()

    def broken_notify(*_a):
        raise RuntimeError("cache write failed")

    monkeypatch.setattr(network_discovery, "notify_cached_model_loaded", broken_notify)
    send = asyncio.create_task(ensure_loaded(_target()))
    await lifecycle["entered"].wait()
    load = local_model_load._LOADS[SERVER_KEY]
    send.cancel()  # Stop: every waiter is gone before the load fails
    with pytest.raises(asyncio.CancelledError):
        await send
    lifecycle["gate"].set()
    await asyncio.wait([load])
    await asyncio.sleep(0)
    assert any("failed unexpectedly" in r.message and r.exc_info for r in caplog.records)


@pytest.mark.asyncio
async def test_omlx_refusing_the_load_is_load_failed_and_a_dead_server_is_unreachable(seat, lifecycle) -> None:
    seat.model("target", 10)
    lifecycle["result"] = {"ok": False, "message": "boom"}
    with pytest.raises(ModelLoadFailed) as refused:
        await ensure_loaded(_target())
    lifecycle["up"] = False  # oMLX dropped while the load ran
    with pytest.raises(ModelLoadFailed) as dropped:
        await ensure_loaded(_target())
    assert (refused.value.cause, dropped.value.cause) == ("load_failed", "unreachable")
    assert seat.readiness().state == "fits_now"  # a failed load marks nothing loaded


@pytest.mark.asyncio
async def test_a_failed_load_leaves_no_stale_task_so_the_next_send_tries_again(seat, lifecycle) -> None:
    seat.model("target", 10)
    lifecycle["result"] = {"ok": False, "message": "boom"}
    with pytest.raises(ModelLoadFailed):
        await ensure_loaded(_target())
    await asyncio.sleep(0)
    assert local_model_load._LOADS == {}
    lifecycle["result"] = {"ok": True}
    await ensure_loaded(_target())
    assert seat.loads == ["target", "target"]


# Background work never loads a model


@pytest.mark.asyncio
async def test_a_completion_to_a_model_that_isnt_loaded_is_refused_before_it_reaches_omlx(monkeypatch) -> None:
    """The one transport every native completion (a turn, a summary, a checkpoint) takes checks oMLX first."""
    from llm import base
    from llm.providers import omlx_lifecycle

    async def not_loaded(*_a, **_k):
        raise omlx_lifecycle.OmlxModelNotLoadedError("model not loaded: target")

    completion = AsyncMock()
    monkeypatch.setattr(omlx_lifecycle, "require_loaded_model", not_loaded)
    monkeypatch.setattr(base, "acompletion", completion)

    response = await base.base_completion(
        [{"role": "user", "content": "hi"}], None, model="openai/target",
        api_key="omlx", api_base="http://127.0.0.1:5100/v1", omlx_server_id=SERVER,
    )

    assert response is not None
    completion.assert_not_awaited()
