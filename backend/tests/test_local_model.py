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
from llm import local_setup_prepare as prepare, local_setup_flow as flow, local_setup_runtime as runtime
from llm.local_model import LocalModel
from llm.local_profiles import MODEL_SETTINGS, STARTER_MODEL
from omlx_recommender_fixtures import FIXTURE, THINKING, best_rows as _best, mac_with as _mac
from omlx_recommender_fixtures import fresh_local_setup  # noqa: F401

_REPO = Path(__file__).resolve().parents[2]
SAMPLING = {"temperature", "top_p", "top_k", "min_p", "presence_penalty", "repetition_penalty", "chat_template_kwargs"}

# What setup picks at each memory size (GiB) from the recorded recommender: oMLX's own
# ranking, first that fits. Stated here so a re-recorded fixture shows its change. Under 24 GiB there is no local model.
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
    first = await flow.begin_local_setup(**kwargs)
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
    row = next(row for row in _best(gib) if row["repo_id"] == repository)
    model = local_model.from_recommendation(row)
    assert model is not None
    assert (model.repository, model.context_window, model.display_name) == (repository, window, name)
    assert view == {"state": "picked", "message": None, "model": model.view()}
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
    operation = local_setup_operation.read()
    assert operation.repository == repository and operation.result == result and operation.downloaded
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


def test_recommender_metadata_survives_in_the_structured_settings_view():
    row = next(row for row in _best(24) if row["repo_id"] == "Jundot/gemma-4-26B-A4B-it-oQ3.5e-mtp")

    model = local_model.from_recommendation(row)

    assert model is not None
    assert model.level == row["level"] and model.fp16 is row["fp16"]
    assert model.relative_speed == row["relative_speed"]
    assert model.view() == {
        "name": model.display_name, "repository": row["repo_id"], "model": model.model,
        "download_size_bytes": row["download_bytes"],
        "download_size_gb": round(row["download_bytes"] / 1000**3, 1),
        "context_window": row["max_context"], "quantization_bits": row["level"],
        "relative_speed": row["relative_speed"], "resident_bytes": row["resident_bytes"],
        "smarts_level": row["smarts_level"], "tokens_per_second": row["tokens_per_second"],
    }
    restored = local_model.from_recommendation(local_model.to_record(model))
    assert restored == model and restored.view() == model.view()
    assert local_setup_operation.Operation.for_model(model, "settings").model.view() == model.view()


def test_resident_bytes_is_kept_only_when_oMLX_gives_a_positive_number():
    row = {"repo_id": "org/big-oQ4e", "download_bytes": 10, "max_context": 65_536}

    assert local_model.from_recommendation(row).resident_bytes is None
    assert "resident_bytes" not in local_model.from_recommendation(row).view()
    assert local_model.from_recommendation({**row, "resident_bytes": 0}).resident_bytes is None
    assert local_model.from_recommendation({**row, "resident_bytes": True}).resident_bytes is None
    assert local_model.from_recommendation({**row, "resident_bytes": 42}).view()["resident_bytes"] == 42


def test_recommendation_rows_below_32k_are_unusable_and_the_window_caps_at_256k():
    rows = [
        {"repo_id": "org/short-oQ4e", "download_bytes": 10, "max_context": 32_767},
        {"repo_id": "org/broken", "download_bytes": True, "max_context": 65_536},
        {"repo_id": "no-owner", "download_bytes": 10, "max_context": 65_536},
        {"repo_id": "org/Long-Model-oQ4e-mtp", "download_bytes": 10, "max_context": 1_048_576},
    ]
    usable = [model for row in rows if (model := local_model.from_recommendation(row)) is not None]
    assert usable == [LocalModel("org/Long-Model-oQ4e-mtp", 10, 262_144)]


def _no_row_reaches_32k(monkeypatch):
    short = [{**row, "max_context": 16_384} for row in _best(48)]
    return _mac(monkeypatch, 48, answers=[{"sections": {"best": short}}] * 2)


def test_when_oMLX_recommends_nothing_welcome_offers_the_starter_model_and_says_why(monkeypatch):
    _no_row_reaches_32k(monkeypatch)

    view = asyncio.run(_prepared())

    assert view["state"] == "picked" and view["model"]["repository"] == STARTER_MODEL.repository
    assert view["model"]["name"] == "Ternary Bonsai 2"
    assert view["message"] == "oMLX didn’t recommend a model for this computer, so CORSIA chose Ternary Bonsai 2."


def test_the_starter_names_itself_from_its_profile_and_keeps_the_name_through_its_records():
    assert STARTER_MODEL.name == "Ternary Bonsai 2" and STARTER_MODEL.display_name == "Ternary Bonsai 2"
    # The name is the profile's, never the repository id's, and the records that carry the model keep it.
    assert LocalModel(STARTER_MODEL.repository, 1, 32_768).display_name == "Ternary Bonsai 2 27B"
    assert local_model.from_recommendation(local_model.to_record(STARTER_MODEL)).display_name == "Ternary Bonsai 2"
    operation = local_setup_operation.Operation.for_model(STARTER_MODEL, "setup")
    assert operation.model.display_name == "Ternary Bonsai 2"


def test_a_recommender_with_no_usable_row_downloads_the_starter_and_writes_no_effort_level(monkeypatch):
    mac = _no_row_reaches_32k(monkeypatch)
    monkeypatch.setitem(THINKING, STARTER_MODEL.model, True)
    monkeypatch.setattr(local_sources, "get_servers", lambda **_kwargs: [])
    asyncio.run(_prepared())

    result = asyncio.run(_flown())

    assert result["ok"] is True and "skipped" not in result
    assert [model.repository for model in mac.downloads] == [STARTER_MODEL.repository]
    [(path, settings)] = mac.omlx.puts()
    assert path == f"/models/{STARTER_MODEL.model}/settings"
    assert settings["max_context_window"] == STARTER_MODEL.context_window
    # Bonsai thinks by default and has an on/off toggle only: nothing is written to turn it on, and no effort level.
    assert not {"enable_thinking", "reasoning_effort"} & set(settings)
    assert local_model.set_up_model() == STARTER_MODEL


def test_a_flight_that_found_no_row_itself_downloads_the_starter(monkeypatch):
    mac = _no_row_reaches_32k(monkeypatch)
    monkeypatch.setitem(THINKING, STARTER_MODEL.model, True)

    result = asyncio.run(_flown())

    assert result["ok"] is True and [model.repository for model in mac.downloads] == [STARTER_MODEL.repository]


def test_a_pick_the_recommender_made_carries_no_starter_note(monkeypatch):
    _mac(monkeypatch, 48)

    view = asyncio.run(_prepared())

    assert view["message"] is None and prepare._prepare.fallback is False


@pytest.mark.parametrize("failure", [httpx.ConnectError("offline"), 504], ids=["offline", "hub-timeout"])
def test_a_recommender_that_cannot_answer_is_couldnt_check_never_the_starter(monkeypatch, failure):
    mac = _mac(monkeypatch, 48, answers=[failure, failure])

    view = asyncio.run(_prepared())

    assert view["state"] == "failed" and view["model"] is None
    assert prepare.fallback_note(STARTER_MODEL) not in str(view["message"])
    assert mac.downloads == [] and prepare._prepare.fallback is False


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
    assert "Couldn't get model recommendations from oMLX" in view["message"] and mac.downloads == []
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

    assert result["ok"] is False and "Couldn't get model recommendations from oMLX" in result["message"]
    assert mac.downloads == []
    operation = local_setup_operation.read()
    assert operation.repository is None and operation.result == result and not operation.downloaded


def test_an_omlx_without_the_recommender_cannot_check(monkeypatch):
    _mac(monkeypatch, 64, answers=[404, 404])

    view = asyncio.run(_prepared())

    assert view["state"] == "failed" and "Couldn't get model recommendations from oMLX" in view["message"]


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
    # Prepare owns this private managed server, so it stops that exact server id.
    assert mac.stopped == ["srv"]
    assert mac.downloads == [] and mac.omlx.puts() == [] and local_setup_operation.read() is None


def test_turning_the_switch_off_after_the_pick_forgets_it(monkeypatch):
    mac = _mac(monkeypatch, 64)
    assert asyncio.run(_prepared())["state"] == "picked"

    view = asyncio.run(prepare.stop_prepare())

    assert view["state"] == "not_checked" and mac.stopped == ["srv"] and mac.downloads == []


def test_the_switch_stops_only_its_private_managed_server(monkeypatch):
    mac = _mac(monkeypatch, 64)

    asyncio.run(_prepared())
    asyncio.run(prepare.stop_prepare())

    assert mac.stopped == ["srv"]



@pytest.mark.parametrize(
    ("system", "gib", "never_local"),
    [("Darwin", 16, False), ("Darwin", 64, False), ("Linux", 16, True), ("Linux", 64, True)],
)
def test_the_local_row_says_a_host_that_is_not_macos_never_runs_a_local_model(monkeypatch, system, gib, never_local):
    _mac(monkeypatch, gib)
    monkeypatch.setattr(local_sources, "get_servers", lambda **_kwargs: [])
    monkeypatch.setattr("llm.local_setup_gates.platform.system", lambda: system)

    source = local_sources.omlx_source((set(), set()))

    # On a Mac memory alone decides ``eligible`` (a low-memory Mac keeps its own message); a host that is
    # not macOS is never eligible, whatever its memory, so a consumer keying on ``eligible`` offers nothing.
    assert source["never_local"] is never_local
    assert source["eligible"] is (gib >= 24 and not never_local)
    if never_local:
        assert source["state"] == "unverified"
        assert source["reason"] == "Local models don’t run on this computer"
        assert source["fix"] is None
        assert source["local_state"] == "stopped"
        assert "settings_download" not in source


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

    asyncio.run(runtime.configure_model_settings("srv", model))

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

    asyncio.run(runtime.configure_model_settings("srv", LocalModel("org/plain-oQ4e", 1, 40_960)))

    [(_path, settings)] = mac.omlx.puts()
    assert "enable_thinking" not in settings and "mtp_enabled" not in settings
    with pytest.raises(RuntimeError, match="does not list Absent"):
        asyncio.run(runtime.configure_model_settings("srv", LocalModel("org/absent-oQ4e", 1, 40_960)))


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
