"""A finished download is pinned within the room for pins, then loaded when it fits now."""

from __future__ import annotations

import asyncio

import pytest

from infra.host_resources import GIB
from llm import local_keep_loaded
from llm.local_model import LocalModel
from llm.local_model_load import seat_lock

MODEL = LocalModel("Jundot/new-model", 1_000_000, 32_768)


@pytest.fixture
def calls(monkeypatch):
    seen = {
        "pins": [], "loads": [], "events": [],
        "can_load": {"ok": True}, "pin": {"ok": True, "settings": {"pinned": True}},
        "load": {"ok": True},
    }

    def set_rows(rows, room_gib):
        async def model_rows(_server_id):
            return rows

        monkeypatch.setattr(local_keep_loaded, "_model_rows", model_rows)
        monkeypatch.setattr(local_keep_loaded, "pin_room_bytes", lambda: room_gib * GIB)

    async def read_loaded(_cfg):
        return []

    async def update_model_settings(_cfg, model, body):
        seen["pins"].append((model, body))
        return seen["pin"]

    async def load_model(_cfg, model):
        seen["loads"].append(model)
        return seen["load"]

    def emit_product_event(category, action, *, properties):
        seen["events"].append((f"{category}.{action}", properties))
        return True

    monkeypatch.setattr(local_keep_loaded, "model_room_bytes", lambda: (100 if seen["can_load"]["ok"] else 4) * GIB)
    monkeypatch.setattr("llm.providers.omlx_lifecycle.read_loaded", read_loaded)
    monkeypatch.setattr(local_keep_loaded, "emit_product_event", emit_product_event)
    monkeypatch.setattr("llm.providers.omlx_settings.update_model_settings", update_model_settings)
    monkeypatch.setattr("llm.providers.omlx_lifecycle.load_model", load_model)
    monkeypatch.setattr("llm.network_discovery.notify_cached_model_loaded", lambda *_a: None)

    async def discover(*_a):
        return None

    monkeypatch.setattr("llm.network_discovery.discover_and_notify", discover)
    monkeypatch.setattr(local_keep_loaded, "_FAILED", {})
    seen["set_rows"] = set_rows
    return seen


def _row(name, gib, pinned=False):
    return {"id": name, "estimated_size": gib * GIB, "pinned": pinned}


@pytest.mark.asyncio
async def test_a_model_that_fits_the_pin_room_is_pinned_then_loaded(calls) -> None:
    calls["set_rows"]([_row("new-model", 10), _row("other", 5, pinned=True)], room_gib=20)

    await local_keep_loaded.keep_loaded("server", MODEL)

    assert calls["pins"] == [("new-model", {"pinned": True})]
    assert calls["loads"] == ["new-model"] and local_keep_loaded.failures("server") == {}
    # The pin route's own event names, marked automatic: CORSIA pinned and loaded it, not the person.
    common = {"provider": "omlx", "model_id": "new-model", "server_id": "server", "automatic": True}
    assert calls["events"] == [("model.pin", common), ("model.load.completed", common)]


@pytest.mark.asyncio
async def test_setup_forces_its_managed_model_to_pin_even_when_the_room_is_full(calls) -> None:
    calls["set_rows"]([_row("new-model", 10), _row("other", 15, pinned=True)], room_gib=20)

    await local_keep_loaded.keep_loaded("server", MODEL, force_pin=True)

    assert calls["pins"] == [("new-model", {"pinned": True})]
    assert calls["loads"] == ["new-model"]


@pytest.mark.asyncio
async def test_a_pin_that_is_not_confirmed_by_the_put_stops_with_its_line(calls) -> None:
    calls["set_rows"]([_row("new-model", 10)], room_gib=20)
    calls["pin"] = {"ok": True, "settings": {"pinned": False}}

    await local_keep_loaded.keep_loaded("server", MODEL)

    assert calls["loads"] == []
    assert local_keep_loaded.failures("server") == {"new-model": local_keep_loaded.NOT_KEPT}


@pytest.mark.asyncio
async def test_pins_already_using_the_room_leave_it_unpinned_and_the_row_says_why_live(calls) -> None:
    calls["set_rows"]([_row("new-model", 10), _row("other", 15, pinned=True)], room_gib=20)

    await local_keep_loaded.keep_loaded("server", MODEL)

    assert calls["pins"] == []
    # It still loads when it fits now. Pins full is no event: annotate_keep_loaded derives it on every read.
    assert calls["loads"] == ["new-model"]
    assert local_keep_loaded.failures("server") == {}


@pytest.mark.asyncio
async def test_a_pin_that_cannot_be_written_stops_with_its_line(calls) -> None:
    calls["set_rows"]([_row("new-model", 10)], room_gib=20)
    calls["pin"] = {"ok": False, "message": "Pin refused"}

    await local_keep_loaded.keep_loaded("server", MODEL)

    assert local_keep_loaded.failures("server") == {"new-model": local_keep_loaded.NOT_KEPT} and calls["loads"] == []
    assert [(name, props.get("automatic"), props.get("error")) for name, props in calls["events"]] == [
        ("model.pin", True, "Pin refused"),
    ]


@pytest.mark.asyncio
async def test_a_pinned_model_without_free_memory_leaves_the_line_to_the_row(calls, monkeypatch, caplog) -> None:
    calls["set_rows"]([_row("new-model", 10)], room_gib=20)
    calls["can_load"] = {"ok": False, "free_gb": 4}
    monkeypatch.setattr(local_keep_loaded, "memory_reading", lambda: "available 8.0 of 32.0 GiB")

    with caplog.at_level("WARNING", logger=local_keep_loaded.__name__):
        outcome = await local_keep_loaded.keep_loaded("server", MODEL)

    assert outcome == {"status": "refused", "verdict": "no_memory", "neighbour_id": None, "line": "Not loaded: needs 10 GB, 4 GB free for a model now"}
    assert calls["pins"] == [("new-model", {"pinned": True})] and calls["loads"] == []
    # The row derives "Not loaded: needs N GB, M GB free for a model now"; the backend writes no second line.
    assert local_keep_loaded.failures("server") == {}
    # The log carries the verdict's inputs, so a refusal on a machine that looked free can be explained.
    assert ("(needs 10.0 GiB, room 4.0 GiB, beside it nothing; available 8.0 of 32.0 GiB)"
            in caplog.text)


@pytest.mark.asyncio
async def test_setup_loads_its_own_model_on_an_empty_seat_past_the_memory_check(calls) -> None:
    calls["set_rows"]([_row("new-model", 10)], room_gib=20)
    calls["can_load"] = {"ok": False, "free_gb": 4}

    outcome = await local_keep_loaded.keep_loaded("server", MODEL, force_pin=True)

    assert outcome == {"status": "loaded"}
    assert calls["loads"] == ["new-model"]


@pytest.mark.asyncio
async def test_setup_past_the_memory_check_still_hears_omlx_refuse(calls) -> None:
    calls["set_rows"]([_row("new-model", 10)], room_gib=20)
    calls["can_load"] = {"ok": False, "free_gb": 4}
    calls["load"] = {"ok": False, "message": "would exceed the memory ceiling"}

    outcome = await local_keep_loaded.keep_loaded("server", MODEL, force_pin=True)

    assert outcome == {"status": "failed", "detail": "would exceed the memory ceiling"}
    assert local_keep_loaded.failures("server") == {"new-model": local_keep_loaded.NOT_LOADED}


@pytest.mark.asyncio
async def test_the_refusal_log_names_what_is_beside_it_in_words(calls, monkeypatch, caplog) -> None:
    calls["set_rows"]([_row("new-model", 10)], room_gib=20)
    monkeypatch.setattr(local_keep_loaded, "model_room_bytes", lambda: 4 * GIB)
    monkeypatch.setattr(local_keep_loaded, "memory_reading", lambda: "reading")
    async def loaded(_cfg):
        return [{"identifier": "one", "size_bytes": 8 * GIB}, {"identifier": "two", "size_bytes": None}]
    monkeypatch.setattr("llm.providers.omlx_lifecycle.read_loaded", loaded)

    with caplog.at_level("WARNING", logger=local_keep_loaded.__name__):
        await local_keep_loaded.keep_loaded("server", MODEL)

    assert "beside it 8.0 GiB, unreported; reading" in caplog.text


def test_the_memory_reading_names_available_total_reserve_and_pages(monkeypatch) -> None:
    from types import SimpleNamespace

    import psutil

    from infra.host_resources import memory_reading

    vm = SimpleNamespace(available=10 * GIB, total=32 * GIB, free=2 * GIB, inactive=8 * GIB, active=20 * GIB)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: vm)

    # A field this platform's psutil lacks (here ``wired``) is left out rather than guessed.
    assert memory_reading() == ("available 10.0 of 32.0 GiB, reserve 4.0 GiB "
                                "(free 2.0, inactive 8.0, active 20.0)")


@pytest.mark.asyncio
async def test_known_neighbour_refuses_with_catalog_line_and_no_room(calls, monkeypatch) -> None:
    calls["set_rows"]([_row("new-model", 10)], room_gib=20)
    monkeypatch.setattr(local_keep_loaded, "model_room_bytes", lambda: 4 * GIB)
    async def loaded(_cfg):
        return [{"identifier": "other", "size_bytes": 8 * GIB}]
    monkeypatch.setattr("llm.providers.omlx_lifecycle.read_loaded", loaded)
    outcome = await local_keep_loaded.keep_loaded("server", MODEL, force_pin=True)
    assert outcome == {"status": "refused", "verdict": "no_room", "neighbour_id": "other",
                       "line": "Not loaded: needs 10 GB, 4 GB free for a model now"}
    assert calls["loads"] == []


@pytest.mark.asyncio
async def test_multiple_neighbours_use_generic_refusal(calls, monkeypatch) -> None:
    calls["set_rows"]([_row("new-model", 10)], room_gib=20)
    monkeypatch.setattr(local_keep_loaded, "model_room_bytes", lambda: 4 * GIB)
    async def loaded(_cfg):
        return [{"identifier": "one", "size_bytes": 8 * GIB}, {"identifier": "two", "size_bytes": 8 * GIB}]
    monkeypatch.setattr("llm.providers.omlx_lifecycle.read_loaded", loaded)
    outcome = await local_keep_loaded.keep_loaded("server", MODEL, force_pin=True)
    assert outcome["verdict"] == "no_room" and outcome["neighbour_id"] is None
    assert calls["loads"] == []


@pytest.mark.asyncio
async def test_unknown_neighbour_refuses_without_catalog_line(calls, monkeypatch) -> None:
    calls["set_rows"]([_row("new-model", 10)], room_gib=20)
    async def loaded(_cfg):
        return [{"identifier": "other", "size_bytes": None}]
    monkeypatch.setattr("llm.providers.omlx_lifecycle.read_loaded", loaded)

    outcome = await local_keep_loaded.keep_loaded("server", MODEL, force_pin=True)

    assert outcome == {"status": "refused", "verdict": "no_room", "neighbour_id": "other", "line": None}
    assert calls["loads"] == [] and local_keep_loaded.failures("server") == {}


@pytest.mark.asyncio
async def test_a_load_oMLX_refuses_says_so_on_the_row(calls) -> None:
    calls["set_rows"]([_row("new-model", 10)], room_gib=20)
    calls["load"] = {"ok": False}

    outcome = await local_keep_loaded.keep_loaded("server", MODEL)
    assert outcome == {"status": "failed", "detail": "Load failed"}

    # The row is pinned, so its switch is already on: the line names the retry from that state.
    assert local_keep_loaded.failures("server") == {
        "new-model": "Not loaded: oMLX couldn’t load it. Turn it off and on again to retry.",
    }
    assert [(name, props["automatic"], props.get("error")) for name, props in calls["events"]] == [
        ("model.pin", True, None),
        ("model.load.failed", True, "Load failed"),
    ]


@pytest.mark.asyncio
async def test_early_pin_exception_propagates_for_setup(calls, monkeypatch) -> None:
    calls["set_rows"]([_row("new-model", 10)], room_gib=20)

    async def fail_pin(*_args):
        raise RuntimeError("private pin detail")

    monkeypatch.setattr("llm.providers.omlx_settings.update_model_settings", fail_pin)
    with pytest.raises(RuntimeError, match="private pin detail"):
        await local_keep_loaded.keep_loaded("server", MODEL, force_pin=True)


@pytest.mark.asyncio
async def test_setup_failure_preserves_existing_load_event(calls) -> None:
    calls["set_rows"]([_row("new-model", 10)], room_gib=20)
    calls["load"] = {"ok": False, "message": "private upstream detail"}

    outcome = await local_keep_loaded.keep_loaded("server", MODEL, force_pin=True)

    assert outcome == {"status": "failed", "detail": "private upstream detail"}
    assert calls["events"][-1][1]["error"] == "private upstream detail"
    assert local_keep_loaded.failures("server")["new-model"] == local_keep_loaded.NOT_LOADED


@pytest.mark.asyncio
async def test_a_refused_load_on_an_unpinned_row_says_turn_it_on(calls) -> None:
    calls["set_rows"]([_row("new-model", 10), _row("other", 15, pinned=True)], room_gib=20)
    calls["load"] = {"ok": False}

    await local_keep_loaded.keep_loaded("server", MODEL)

    # Pins full left it unpinned, so its switch is off: turning it on is the retry.
    assert calls["pins"] == []
    assert local_keep_loaded.failures("server") == {
        "new-model": "Not loaded: oMLX couldn’t load it. Turn it on to try again.",
    }


@pytest.mark.asyncio
async def test_setup_pins_and_loads_unsized_model_on_an_empty_seat(calls) -> None:
    calls["set_rows"]([{"id": "new-model"}], room_gib=20)

    await local_keep_loaded.keep_loaded("server", MODEL, force_pin=True)

    assert calls["pins"] == [("new-model", {"pinned": True})]
    assert calls["loads"] == ["new-model"] and local_keep_loaded.failures("server") == {}


@pytest.mark.asyncio
async def test_unsized_model_loads_on_an_empty_seat_without_pinning(calls) -> None:
    calls["set_rows"]([{"id": "new-model"}], room_gib=20)

    await local_keep_loaded.keep_loaded("server", MODEL)

    # The empty seat cannot lose another model; the no-size line still describes pinning.
    assert calls["pins"] == [] and calls["loads"] == ["new-model"]
    assert [event[0] for event in calls["events"]] == ["model.load.completed"]
    assert local_keep_loaded.failures("server") == {}


@pytest.mark.asyncio
async def test_an_unreadable_model_list_says_it_could_not_keep_it_loaded(calls) -> None:
    calls["set_rows"](None, room_gib=20)

    await local_keep_loaded.keep_loaded("server", MODEL)

    assert calls["pins"] == [] and calls["loads"] == []
    assert local_keep_loaded.failures("server") == {
        "new-model": "Downloaded, but couldn’t keep it loaded. Turn it on to try again.",
    }


@pytest.mark.asyncio
async def test_a_failed_memory_check_says_so_on_the_row(calls, monkeypatch) -> None:
    calls["set_rows"]([_row("new-model", 10)], room_gib=20)

    def broken():
        raise RuntimeError("memory unreadable")

    monkeypatch.setattr(local_keep_loaded, "model_room_bytes", broken)

    await local_keep_loaded.keep_loaded("server", MODEL)

    assert calls["loads"] == [] and local_keep_loaded.failures("server") == {"new-model": local_keep_loaded.NOT_LOADED}


@pytest.mark.asyncio
async def test_a_new_attempt_replaces_the_last_failure_and_servers_stay_apart(calls) -> None:
    calls["set_rows"]([_row("new-model", 10)], room_gib=20)
    calls["load"] = {"ok": False}
    await local_keep_loaded.keep_loaded("server", MODEL)
    await local_keep_loaded.keep_loaded("other-server", MODEL)
    calls["load"] = {"ok": True}

    await local_keep_loaded.keep_loaded("server", MODEL)

    assert local_keep_loaded.failures("server") == {}
    assert local_keep_loaded.failures("other-server") == {"new-model": local_keep_loaded.NOT_LOADED}
    local_keep_loaded.forget_failure("other-server", "new-model")
    assert local_keep_loaded.failures("other-server") == {}


@pytest.mark.asyncio
async def test_a_download_load_waits_for_a_send_load_on_the_same_seat(calls) -> None:
    calls["set_rows"]([_row("new-model", 10)], room_gib=20)
    async with seat_lock("server"):  # a send's load holds the seat
        keep = asyncio.create_task(local_keep_loaded.keep_loaded("server", MODEL))
        await asyncio.sleep(0.05)
        assert calls["loads"] == []  # the memory check and the load wait for the seat
    await keep
    assert calls["loads"] == ["new-model"]
