from __future__ import annotations

import asyncio

import pytest

from llm import local_setup, local_setup_operation


@pytest.fixture(autouse=True)
def isolated_operation(monkeypatch, tmp_path):
    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
    monkeypatch.setattr(local_setup_operation, "_state_root", lambda: tmp_path)
    monkeypatch.setattr(local_setup, "_emit", lambda *_args, **_kwargs: None)


def test_recovery_follows_the_recorded_task_without_posting_another_download(monkeypatch):
    profile = local_setup.DEFAULT_PROFILE
    local_setup_operation.write(local_setup_operation.Operation(profile.id, "setup", "task-1"))
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

    asyncio.run(local_setup._wait_for_download("server", lambda _stage: None, profile))

    assert calls == [("GET", "/hf/tasks")]


def test_restart_resumes_the_persisted_kind_under_the_operation_lock(monkeypatch):
    profile = local_setup.DEFAULT_PROFILE
    local_setup_operation.write(local_setup_operation.Operation(profile.id, "starter", "task-1"))
    observed: dict[str, object] = {}

    monkeypatch.setattr(local_setup, "host_ram_bytes", lambda: 128 * 1024**3)
    monkeypatch.setattr(local_setup, "profile_eligibility", lambda *_args: local_setup.Eligibility(32_768, True))

    async def run_flight(eligibility, *, profile, into_own_omlx=False, on_stage=None):
        observed.update({"profile": profile.id, "starter": into_own_omlx, "eligible": eligibility.eligible})
        local_setup_operation.clear()
        local_setup._release_operation_lock()
        return {"ok": True}

    monkeypatch.setattr(local_setup, "_run_flight", run_flight)

    async def scenario():
        assert await local_setup.resume_pending_setup() is True
        await local_setup._flight.task

    asyncio.run(scenario())

    assert observed == {"profile": profile.id, "starter": True, "eligible": True}


def test_ineligible_recovery_record_is_discarded_before_fresh_setup(monkeypatch):
    stale_profile = local_setup.profile_by_id("qwen-27b-oq4e")
    assert stale_profile is not None
    local_setup_operation.write(local_setup_operation.Operation(stale_profile.id, "setup", "task-1"))
    monkeypatch.setattr(local_setup, "host_ram_bytes", lambda: 32 * 1024**3)

    async def run_flight(*_args, **_kwargs):
        return {"ok": True}

    monkeypatch.setattr(local_setup, "_run_flight", run_flight)
    monkeypatch.setattr(local_setup, "_claim_operation", lambda *_args: True)

    async def scenario():
        assert await local_setup.resume_pending_setup() is False
        assert local_setup_operation.read() is None
        result = await local_setup.begin_local_setup()
        assert result["in_progress"] is True
        await local_setup._flight.task

    asyncio.run(scenario())
