"""The local model is oMLX's recommender pick: per memory size, the gates, failures, thinking.

No oMLX runs: ``omlx_recommender_fixtures`` answers with the recorded oMLX
v0.7.1rc1 recommender output and replaces every install, start and download.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import httpx
import pytest

from llm import local_model, local_setup, local_setup_operation, local_sources
from llm import local_setup_prepare as prepare
from llm.local_model import LocalModel
from llm.local_profiles import MODEL_SETTINGS
from omlx_recommender_fixtures import FIXTURE, THINKING, best_rows as _best, mac_with as _mac
from omlx_recommender_fixtures import fresh_local_setup  # noqa: F401

_REPO = Path(__file__).resolve().parents[2]
SAMPLING = {"temperature", "top_p", "top_k", "min_p", "presence_penalty", "repetition_penalty", "chat_template_kwargs"}

# What the recorded recommender picks at each memory size (GiB), stated here so a
# re-recorded fixture shows its change. Under 24 GiB there is no local model.
PICKS = {
    16: None,
    24: ("Jundot/gemma-4-26B-A4B-it-oQ3.5e-mtp", 90_890, "Gemma 4 26B A4B"),
    32: ("Jundot/Qwen3.8-27B-oQ4e-mtp", 81_651, "Qwen3.8 27B"),
    48: ("Jundot/Qwen3.8-27B-oQ4e-mtp", 262_144, "Qwen3.8 27B"),
    64: ("Jundot/Qwen3.8-27B-oQ4e-mtp", 262_144, "Qwen3.8 27B"),
    96: ("Jundot/Qwen3.8-Flash-Next-oQ3.5e-mtp", 127_831, "Qwen3.8 Flash Next"),
    128: ("Jundot/Qwen3.8-Flash-Next-oQ4e-mtp", 262_144, "Qwen3.8 Flash Next"),
    192: ("Jundot/Qwen3.8-Flash-Next-oQ4e-mtp", 262_144, "Qwen3.8 Flash Next"),
}
FITTING = [gib for gib, pick in PICKS.items() if pick is not None]


@pytest.fixture(autouse=True)
def fresh(fresh_local_setup):
    yield


async def _prepared() -> dict:
    """Welcome opens with the switch on: start the pick and return what its request answers once it settles."""
    await prepare.begin_prepare()
    return await prepare.settled_view()


async def _flown(**kwargs) -> dict:
    """Get started: run the setup flight to its result."""
    first = await local_setup.begin_local_setup(**kwargs)
    task = local_setup._flight.task
    return await task if task is not None else first


def test_every_oMLX_pin_site_names_the_same_release():
    script = (_REPO / "desktop" / "scripts" / "build-macos-bundle.sh").read_text()
    bundled = re.search(r'^(?:readonly )?OMLX_VERSION="([^"]+)"', script, re.MULTILINE).group(1)
    assert bundled == local_model.OMLX_PIN
    assert FIXTURE["tag"] == f"v{local_model.OMLX_PIN}"
    assert local_model.omlx_has_recommender(local_model.OMLX_PIN)
    assert not local_model.omlx_has_recommender("0.7.0") and not local_model.omlx_has_recommender(None)


def test_the_recording_covers_each_memory_size_once():
    assert sorted(int(gib) for gib in FIXTURE["results"]) == sorted(PICKS)


@pytest.mark.parametrize("gib", sorted(PICKS))
def test_welcome_shows_the_recommenders_pick_for_each_memory_size(monkeypatch, gib):
    mac = _mac(monkeypatch, gib)

    view = asyncio.run(_prepared())

    if PICKS[gib] is None:
        assert prepare.NO_LOCAL_MODEL == "This computer can't run a local model well. Connect a subscription or an API key in the app."
        assert view == {"state": "low_memory", "model": None, "message": prepare.NO_LOCAL_MODEL}
        assert mac.started == [] and mac.omlx.calls == []
        return
    repository, window, name = PICKS[gib]
    size = next(row["download_bytes"] for row in _best(gib) if row["repo_id"] == repository)
    assert view == {"state": "picked", "message": None, "model": {
        "name": name, "repository": repository, "download_size_bytes": size,
        "download_size_gb": round(size / 1000**3, 1), "context_window": window,
    }}
    # Welcome only picks: it asks once and downloads nothing.
    assert mac.omlx.asks() == 1 and mac.downloads == [] and mac.omlx.puts() == []


@pytest.mark.parametrize("gib", FITTING)
def test_get_started_downloads_the_pick_and_every_reader_sees_that_model(monkeypatch, gib):
    mac = _mac(monkeypatch, gib)
    monkeypatch.setattr(local_sources, "get_servers", lambda **_kwargs: [])
    repository, window, _name = PICKS[gib]
    asyncio.run(_prepared())

    result = asyncio.run(_flown())

    assert result["ok"] is True and result["model"] == repository.split("/")[1]
    # The flight took Welcome's pick rather than asking again, and downloaded only it.
    assert mac.omlx.asks() == 1 and [model.repository for model in mac.downloads] == [repository]
    [(path, settings)] = mac.omlx.puts()
    assert path == f"/models/{repository.split('/')[1]}/settings" and settings["max_context_window"] == window
    recorded = local_model.set_up_model()
    assert (recorded.repository, recorded.context_window) == (repository, window)
    assert local_setup_operation.read() is None
    # A later server process has no prepare state; it reads the same model from the record.
    monkeypatch.setattr(prepare, "_prepare", prepare._Prepare())
    assert asyncio.run(prepare.pick_view())["model"]["repository"] == repository
    source = local_sources.omlx_source()
    assert source["context_window"] == window and source["eligible"] is True


def test_the_flight_asks_the_recommender_itself_when_welcome_did_not(monkeypatch):
    mac = _mac(monkeypatch, 48)

    result = asyncio.run(_flown())

    assert result["ok"] is True and mac.omlx.asks() == 1
    assert [model.repository for model in mac.downloads] == ["Jundot/Qwen3.8-27B-oQ4e-mtp"]


def test_the_pick_skips_rows_below_32k_and_caps_the_window_at_256k():
    rows = [
        {"repo_id": "org/short-oQ4e", "download_bytes": 10, "max_context": 32_767},
        {"repo_id": "org/broken", "download_bytes": True, "max_context": 65_536},
        {"repo_id": "no-owner", "download_bytes": 10, "max_context": 65_536},
        {"repo_id": "org/Long-Model-oQ4e-mtp", "download_bytes": 10, "max_context": 1_048_576},
    ]
    assert local_model.pick({"sections": {"best": rows}}) == LocalModel("org/Long-Model-oQ4e-mtp", 10, 262_144)
    assert local_model.pick({"sections": {"best": rows[:3]}}) is None
    assert local_model.pick({"sections": {}}) is None and local_model.pick([]) is None


def test_when_nothing_reaches_32k_welcome_says_so_and_get_started_downloads_nothing(monkeypatch):
    short = [{**row, "max_context": 16_384} for row in _best(48)]
    mac = _mac(monkeypatch, 48, answers=[{"sections": {"best": short}}] * 2)

    view = asyncio.run(_prepared())
    assert view == {"state": "none_fits", "model": None, "message": prepare.NO_LOCAL_MODEL}

    result = asyncio.run(_flown())

    assert result["ok"] is True and result["skipped"] == "none_fits"
    assert result["message"] == prepare.NO_LOCAL_MODEL
    assert mac.downloads == [] and mac.omlx.puts() == []
    assert local_model.set_up_model() is None and local_setup_operation.read() is None


@pytest.mark.parametrize("first", [504, 503, 502, httpx.ConnectError("connection refused")],
                         ids=["504", "503", "502", "refused"])
def test_a_recommender_that_cannot_answer_is_asked_once_more_after_omlx_is_ready(monkeypatch, first):
    mac = _mac(monkeypatch, 64, answers=[first])

    view = asyncio.run(_prepared())

    assert view["state"] == "picked" and view["model"]["repository"] == "Jundot/Qwen3.8-27B-oQ4e-mtp"
    assert mac.omlx.asks() == 2 and mac.health == [5200]


def test_a_second_failure_shows_cant_check_then_try_again_picks(monkeypatch):
    mac = _mac(monkeypatch, 64, answers=[502, 504])

    view = asyncio.run(_prepared())

    assert view["state"] == "failed" and view["model"] is None
    assert "HTTP 504" in view["message"] and mac.downloads == []
    # Try again starts the pick afresh.
    assert asyncio.run(_prepared())["state"] == "picked" and mac.omlx.asks() == 3


def test_get_started_after_cant_check_asks_again_and_downloads_the_pick(monkeypatch):
    mac = _mac(monkeypatch, 64, answers=[502, 502])
    assert asyncio.run(_prepared())["state"] == "failed"

    result = asyncio.run(_flown())

    assert result["ok"] is True and mac.omlx.asks() == 3
    assert [model.repository for model in mac.downloads] == ["Jundot/Qwen3.8-27B-oQ4e-mtp"]


def test_a_flight_whose_recommender_never_answers_fails_without_downloading(monkeypatch):
    mac = _mac(monkeypatch, 64, answers=[502, 502])

    result = asyncio.run(_flown())

    assert result["ok"] is False and "HTTP 502" in result["message"]
    assert mac.downloads == [] and local_setup_operation.read() is None


def test_an_omlx_without_the_recommender_cannot_check(monkeypatch):
    _mac(monkeypatch, 64, answers=[404, 404])

    view = asyncio.run(_prepared())

    assert view["state"] == "failed" and "HTTP 404" in view["message"]


def test_overlapping_prepare_requests_start_one_task(monkeypatch):
    _mac(monkeypatch, 64)
    runs = []

    async def run():
        runs.append(True)
        await asyncio.Event().wait()

    async def exercise():
        first, second = await asyncio.gather(prepare.begin_prepare(), prepare.begin_prepare())
        assert first["state"] == second["state"] == "checking"
        await prepare.stop_prepare()

    monkeypatch.setattr(prepare, "_run", run)
    asyncio.run(exercise())
    assert runs == [True]


def test_turning_the_switch_off_while_checking_stops_the_pick_and_downloads_nothing(monkeypatch):
    mac = _mac(monkeypatch, 64)

    async def run():
        mac.omlx.gate = asyncio.Event()
        assert (await prepare.begin_prepare())["state"] == "checking"
        waiting = asyncio.create_task(prepare.settled_view())
        while mac.omlx.asks() == 0:
            await asyncio.sleep(0)
        assert (await prepare.pick_view())["state"] == "checking" and not waiting.done()
        return await prepare.stop_prepare(), await waiting

    view, answered = asyncio.run(run())

    # Welcome's waiting request answers too, so nothing is left hanging on a stopped pick.
    assert view == answered == {"state": "not_checked", "model": None, "message": None}
    # Prepare started that oMLX, so it stops it; nothing was downloaded or written.
    assert mac.commands == [["omlx", "stop"]]
    assert mac.downloads == [] and mac.omlx.puts() == [] and local_setup_operation.read() is None


def test_turning_the_switch_off_after_the_pick_forgets_it(monkeypatch):
    mac = _mac(monkeypatch, 64)
    assert asyncio.run(_prepared())["state"] == "picked"

    view = asyncio.run(prepare.stop_prepare())

    assert view["state"] == "not_checked" and mac.commands == [["omlx", "stop"]] and mac.downloads == []


def test_the_switch_leaves_an_omlx_that_was_already_answering_running(monkeypatch):
    mac = _mac(monkeypatch, 64)

    async def answering(_port):
        return True

    monkeypatch.setattr(local_setup, "_omlx_answers", answering)
    asyncio.run(_prepared())
    asyncio.run(prepare.stop_prepare())

    assert mac.commands == []


def test_welcome_never_starts_a_second_omlx_beside_the_persons_own(monkeypatch):
    mac = _mac(monkeypatch, 64)
    monkeypatch.setattr(local_setup, "existing_install_is_foreign", lambda: True)

    view = asyncio.run(_prepared())

    assert view == {"state": "own_omlx", "model": None, "message": None}
    assert mac.started == [] and mac.omlx.calls == []


@pytest.mark.parametrize("gib, allowed", [(23.9, False), (24, True)])
def test_the_24_gib_gate_decides_whether_there_is_a_local_model(monkeypatch, gib, allowed):
    mac = _mac(monkeypatch, gib)
    monkeypatch.setattr(local_sources, "get_servers", lambda **_kwargs: [])

    view = asyncio.run(_prepared())
    result = asyncio.run(_flown())

    assert (view["state"] == "picked") is allowed
    assert result["ok"] is allowed and bool(mac.downloads) is allowed
    assert local_sources.omlx_source()["eligible"] is allowed
    if not allowed:
        assert view["state"] == "low_memory" and mac.started == [] and mac.omlx.calls == []


@pytest.mark.parametrize("model_id", sorted(THINKING))
def test_each_picked_family_thinks_on_an_agent_turn_with_its_own_sampling(monkeypatch, model_id):
    mac = _mac(monkeypatch, 64)
    model = LocalModel(f"Jundot/{model_id}", 1, 65_536)

    asyncio.run(local_setup._configure_model_settings("srv", model))

    [(path, settings)] = mac.omlx.puts()
    assert path == f"/models/{model_id}/settings"
    # Gemma 4 thinks only when asked; Qwen 3.x thinks unless told not to, so it gets no override.
    written = settings.get("enable_thinking")
    assert written is (True if THINKING[model_id] is False else None)
    assert (written if written is not None else THINKING[model_id]) is True
    # Sampling stays the model's own; the window is the recommender's; the -mtp head is on.
    assert not SAMPLING & settings.keys()
    assert settings["max_context_window"] == 65_536 and settings["mtp_enabled"] is True
    # The model-independent settings are written as they are.
    assert {key: settings[key] for key in MODEL_SETTINGS} == MODEL_SETTINGS


def test_a_model_with_no_thinking_toggle_gets_none_and_a_missing_model_fails(monkeypatch):
    mac = _mac(monkeypatch, 64)
    monkeypatch.setitem(THINKING, "plain-oQ4e", None)

    asyncio.run(local_setup._configure_model_settings("srv", LocalModel("org/plain-oQ4e", 1, 40_960)))

    [(_path, settings)] = mac.omlx.puts()
    assert "enable_thinking" not in settings and "mtp_enabled" not in settings
    with pytest.raises(RuntimeError, match="does not list Absent"):
        asyncio.run(local_setup._configure_model_settings("srv", LocalModel("org/absent-oQ4e", 1, 40_960)))


def test_the_set_up_record_round_trips_and_a_bad_one_reads_as_absent():
    model = LocalModel("Jundot/Qwen3.8-27B-oQ4e-mtp", 16_971_681_558, 262_144)
    assert local_model.set_up_model() is None

    local_model.record_set_up(model)

    assert local_model.set_up_model() == model
    local_model._record_path().write_text('{"repo_id": "Jundot/x", "download_bytes": 1, "max_context": 512}')
    assert local_model.set_up_model() is None
    # The flat shape unreleased builds wrote reads as absent too.
    local_model._record_path().write_text(
        '{"repository": "Jundot/x", "download_size_bytes": 1, "context_window": 262144}')
    assert local_model.set_up_model() is None
    local_model._record_path().write_text("not json")
    assert local_model.set_up_model() is None


def test_display_names_drop_the_owner_and_build_suffix():
    names = {
        "Jundot/Qwen3.8-27B-oQ4e-mtp": "Qwen3.8 27B",
        "Jundot/gemma-4-26B-A4B-it-oQ3.5e-mtp": "Gemma 4 26B A4B",
        "prism-ml/Ternary-Bonsai-2-27B-mlx-2bit": "Ternary Bonsai 2 27B",
    }
    assert {repo: LocalModel(repo, 1, 32_768).display_name for repo in names} == names
