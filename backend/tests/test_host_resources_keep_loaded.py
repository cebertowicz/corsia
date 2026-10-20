"""host_resources owns the keep-loaded verdicts and their sentences; every reader asks it."""

from __future__ import annotations

import asyncio

import pytest

from infra import host_resources
from infra.host_resources import GIB, annotate_keep_loaded, fits_now, not_loaded_line, pin_fits, pins_full_line


def _model(name, gib, *, pinned=False, loaded=False, starting=False):
    return {"id": name, "size_bytes": int(gib * GIB), "pinned": pinned, "loaded": loaded, "starting": starting}


@pytest.fixture
def room(monkeypatch):
    """A Mac with 20 GB for pins and 8 GB free for a model now."""
    monkeypatch.setattr(host_resources, "pin_room_bytes", lambda: 20 * GIB)
    monkeypatch.setattr(host_resources, "model_room_bytes", lambda: 8 * GIB)


def test_a_model_fits_now_up_to_exactly_the_room_a_model_may_take() -> None:
    assert fits_now(8, 8) is True
    assert fits_now(8.01, 8) is False


def test_pins_fit_the_room_up_to_exactly_the_room_for_pins() -> None:
    assert pin_fits(6 * GIB, 14 * GIB, 20 * GIB) is True
    assert pin_fits(6 * GIB + 1, 14 * GIB, 20 * GIB) is False


def test_the_pins_full_sentence_has_one_writer_and_adds_the_loaded_note_only_for_a_loaded_model() -> None:
    base = "Models kept loaded use 14 of 20 GB. Turn one off to keep this one loaded."

    assert pins_full_line(14 * GIB, 20 * GIB) == base
    assert pins_full_line(14 * GIB, 20 * GIB, loaded=True) == f"{base} It's loaded, but won't load at the next start."


def test_the_not_loaded_sentence_rounds_the_need_up_and_is_absent_when_it_fits() -> None:
    assert not_loaded_line(int(11.2 * GIB), 8 * GIB) == "Not loaded: needs 12 GB, 8 GB free for a model now"
    assert not_loaded_line(8 * GIB, 8 * GIB) is None


@pytest.mark.asyncio
async def test_the_load_check_applies_the_same_fits_now_rule(monkeypatch) -> None:
    async def resources():
        return {"free_gb": 20.0, "safe_free_gb": 8.0, "safe_reserve_gb": 8.0}

    monkeypatch.setattr(host_resources, "get_host_resources", resources)

    assert (await host_resources.check_can_load(8.0, "omlx"))["ok"] is True
    assert (await host_resources.check_can_load(8.5, "omlx"))["ok"] is False


def test_a_loaded_model_the_pins_cannot_take_carries_the_pins_full_line(room) -> None:
    models = [_model("loaded-one", 8, loaded=True), _model("pinned-a", 14, pinned=True, loaded=True)]

    annotate_keep_loaded(models)

    assert models[0]["pin_fits"] is False
    assert models[0]["keep_line"] == pins_full_line(14 * GIB, 20 * GIB, loaded=True)
    assert models[1]["pin_fits"] is True
    assert "keep_line" not in models[1]


def test_an_unloaded_model_the_pins_cannot_take_carries_the_pins_full_line_too(room) -> None:
    models = [_model("not-loaded", 8), _model("pinned-a", 14, pinned=True, loaded=True)]

    annotate_keep_loaded(models)

    assert models[0]["pin_fits"] is False
    assert models[0]["keep_line"] == pins_full_line(14 * GIB, 20 * GIB)


def test_the_pins_full_line_follows_the_pins_live(room) -> None:
    """Turning a pin off frees the room on the very next read: no saved copy can contradict it."""
    full = [_model("waiting", 8), _model("pinned-a", 14, pinned=True, loaded=True)]
    freed = [_model("waiting", 8), _model("pinned-a", 14, loaded=True)]

    annotate_keep_loaded(full)
    annotate_keep_loaded(freed)

    assert "keep_line" in full[0]
    assert freed[0]["pin_fits"] is True and "keep_line" not in freed[0]


def test_a_failed_keep_attempt_is_the_rows_line_over_the_live_verdicts(room) -> None:
    models = [_model("failed", 8), _model("pinned-a", 14, pinned=True, loaded=True), {"id": "unsized"}]

    annotate_keep_loaded(models, {"failed": "couldn't load", "unsized": "couldn't keep"})

    assert models[0]["keep_line"] == "couldn't load" and models[0]["pin_fits"] is False
    assert models[2] == {"id": "unsized", "keep_line": "couldn't keep"}


def test_a_pinned_model_that_does_not_fit_now_carries_the_not_loaded_line(room) -> None:
    models = [_model("pinned-big", 12, pinned=True)]

    annotate_keep_loaded(models)

    assert models[0]["keep_line"] == "Not loaded: needs 12 GB, 8 GB free for a model now"


def test_a_pinned_model_oMLX_is_still_loading_says_nothing(room) -> None:
    models = [_model("starting-up", 12, pinned=True, starting=True)]

    annotate_keep_loaded(models)

    assert "keep_line" not in models[0]


def test_a_row_without_a_size_gets_no_verdict(room) -> None:
    models = [{"id": "unsized", "pinned": True, "loaded": False}]

    annotate_keep_loaded(models)

    assert "pin_fits" not in models[0] and "keep_line" not in models[0]


def test_a_model_that_fits_and_is_unpinned_needs_no_line(room) -> None:
    models = [_model("plain", 4)]

    annotate_keep_loaded(models)

    assert models[0]["pin_fits"] is True
    assert "keep_line" not in models[0]


def test_a_download_will_pin_by_the_same_rule(monkeypatch) -> None:
    from llm import local_keep_loaded, local_sources

    async def rows(_server_id):
        return [{"id": "a", "estimated_size": 14 * GIB, "pinned": True}]

    async def run_in_db(fn, *args):
        return fn(*args)

    monkeypatch.setattr(local_keep_loaded, "_model_rows", rows)
    monkeypatch.setattr(local_keep_loaded, "pin_room_bytes", lambda: 20 * GIB)
    monkeypatch.setattr(local_sources, "get_servers", lambda **_kwargs: [{"id": "managed", "status": "online"}])
    monkeypatch.setattr(local_sources, "is_corsia_managed_server", lambda _server: True)
    monkeypatch.setattr("infra.db_async.run_in_db", run_in_db)

    def sources(size_gib):
        return [{"source": "omlx", "settings_download": {"size_bytes": size_gib * GIB, "failed": False}}]

    fits, full = sources(6), sources(7)
    asyncio.run(local_keep_loaded.annotate_download_will_pin(fits))
    asyncio.run(local_keep_loaded.annotate_download_will_pin(full))

    assert fits[0]["settings_download"]["will_pin"] is True
    assert full[0]["settings_download"]["will_pin"] is False
