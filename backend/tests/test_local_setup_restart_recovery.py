from __future__ import annotations

import asyncio
import json

import pytest

from llm import local_setup, local_setup_operation, local_setup_new_omlx
from llm.local_model import LocalModel

MODEL = LocalModel("Jundot/Qwen3.8-27B-oQ4e-mtp", 16_971_681_558, 262_144)


@pytest.fixture(autouse=True)
def isolated_operation(monkeypatch, tmp_path):
    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
    monkeypatch.setattr(local_setup_operation, "_state_root", lambda: tmp_path)
    monkeypatch.setattr(local_setup, "_emit", lambda *_args, **_kwargs: None)


def test_recovery_follows_the_recorded_task_without_posting_another_download(monkeypatch):
    local_setup_operation.write(local_setup_operation.Operation.for_model(MODEL, "setup", "task-1"))
    calls: list[tuple[str, str]] = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"tasks": [{"task_id": "task-1", "status": "completed"}]}

    async def admin_request(_cfg, method, path, **_kwargs):
        calls.append((method, path))
        return Response()

    monkeypatch.setattr("llm.providers.omlx_endpoints.admin_request", admin_request)

    asyncio.run(local_setup._wait_for_download("server", lambda _stage: None, MODEL))

    assert calls == [("GET", "/hf/tasks")]


def test_restart_resumes_the_recorded_setup_model_under_the_operation_lock(monkeypatch):
    local_setup_operation.write(local_setup_operation.Operation.for_model(MODEL, "setup", "task-1"))
    observed: dict[str, object] = {}
    monkeypatch.setattr(local_setup_new_omlx, "local_memory_ok", lambda *_args: True)

    async def run_flight(*, model=None, on_stage=None):
        observed.update({"model": model, "locked": local_setup._flight.operation_lock})
        local_setup_operation.clear()
        local_setup._release_operation_lock()
        return {"ok": True}

    monkeypatch.setattr(local_setup_new_omlx, "_run_flight", run_flight)

    async def scenario():
        assert await local_setup_new_omlx.resume_pending_setup() is True
        # Welcome and the sources panel show the resumed model while it downloads.
        assert local_setup.flight_model() == MODEL
        await local_setup._flight.task

    asyncio.run(scenario())

    assert observed["model"] == MODEL
    assert observed["locked"] is not None


def test_restart_dispatches_a_settings_record_to_its_own_completion_path(monkeypatch):
    local_setup_operation.write(local_setup_operation.Operation.for_model(MODEL, "settings", "task-1"))
    monkeypatch.setattr(local_setup_new_omlx, "local_memory_ok", lambda *_args: True)
    observed: list[LocalModel] = []

    async def resume_settings(model):
        observed.append(model)
        return {"ok": True}

    monkeypatch.setattr("llm.local_settings_download._run_flight", resume_settings)

    async def scenario():
        assert await local_setup_new_omlx.resume_pending_setup() is True
        await local_setup._flight.task

    asyncio.run(scenario())
    assert observed == [MODEL]


def test_setup_during_a_settings_recovery_stays_in_progress_and_never_adopts_the_model(monkeypatch):
    local_setup_operation.write(local_setup_operation.Operation.for_model(MODEL, "settings", "task-1"))
    monkeypatch.setattr(local_setup_new_omlx, "local_memory_ok", lambda *_args: True)
    recorded: list[LocalModel] = []

    async def resume_settings(_model):
        await release.wait()
        return {"ok": True}

    release = asyncio.Event()
    monkeypatch.setattr("llm.local_settings_download._run_flight", resume_settings)
    monkeypatch.setattr("llm.local_model.record_set_up", recorded.append)

    async def scenario():
        result = await local_setup_new_omlx.begin_local_setup()
        assert result["in_progress"] is True
        assert local_setup._flight.kind == "settings"
        assert local_setup._flight.model == MODEL
        assert recorded == []
        release.set()
        await local_setup._flight.task

    asyncio.run(scenario())
    assert recorded == []


def test_a_record_on_a_mac_now_below_24_gib_is_discarded(monkeypatch):
    local_setup_operation.write(local_setup_operation.Operation.for_model(MODEL, "setup", "task-1"))
    monkeypatch.setattr(local_setup_new_omlx, "local_memory_ok", lambda *_args: False)

    assert asyncio.run(local_setup_new_omlx.resume_pending_setup()) is False
    assert local_setup_operation.read() is None and not local_setup_operation.record_path().exists()


def test_a_record_from_before_the_recommender_is_ignored_and_a_fresh_setup_starts(monkeypatch):
    # A record naming a profile id has no model to resume, so setup starts over and picks.
    local_setup_operation.record_path().write_text(
        json.dumps({"profile_id": "qwen-27b-oq4e", "kind": "setup", "task_id": "task-1"}))
    monkeypatch.setattr(local_setup_new_omlx, "local_memory_ok", lambda *_args: True)

    started: list[object] = []

    async def run_flight(*, model=None, **_kwargs):
        started.append(model)
        return {"ok": True}

    monkeypatch.setattr(local_setup_new_omlx, "_run_flight", run_flight)

    async def scenario():
        assert await local_setup_new_omlx.resume_pending_setup() is False
        result = await local_setup_new_omlx.begin_local_setup()
        assert result["in_progress"] is True
        await local_setup._flight.task
        local_setup._release_operation_lock()

    asyncio.run(scenario())

    assert started == [None]


@pytest.mark.parametrize("onboarding", [True, False])
def test_restart_preserves_the_download_origin(monkeypatch, onboarding):
    local_setup_operation.write(local_setup_operation.Operation.for_model(
        MODEL, "setup", "task-origin", onboarding=onboarding,
    ))
    monkeypatch.setattr(local_setup_new_omlx, "local_memory_ok", lambda: True)
    observed = []

    async def resumed(*, model):
        observed.append((model, local_setup._flight.onboarding))
        local_setup.finish_flight({"ok": True})
        return {"ok": True}

    monkeypatch.setattr(local_setup_new_omlx, "_run_flight", resumed)

    async def scenario():
        assert await local_setup_new_omlx.resume_pending_setup()
        await local_setup.flight_task()

    asyncio.run(scenario())
    assert observed == [(MODEL, onboarding)]
