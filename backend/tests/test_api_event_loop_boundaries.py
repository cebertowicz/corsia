from __future__ import annotations

import asyncio
import threading
import time

import pytest


@pytest.fixture(autouse=True)
def _require_stale_refresh_stub(monkeypatch):
    """Keep a real catalog refresh from starting in these event-loop tests."""
    import connections.harness_refresh as harness_refresh

    def _unmocked_stale_refresh() -> None:
        pytest.fail(
            "test must stub schedule_stale_refresh before it starts a harness check",
        )

    monkeypatch.setattr(
        harness_refresh, "schedule_stale_refresh", _unmocked_stale_refresh,
    )


@pytest.mark.asyncio
async def test_models_catalog_requires_stale_refresh_stub():
    import api.models as api_models

    with pytest.raises(
        pytest.fail.Exception,
        match="test must stub schedule_stale_refresh",
    ):
        await api_models.list_models()


async def _heartbeat_delay() -> float:
    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.sleep(0.01)
    return loop.time() - started


@pytest.mark.asyncio
async def test_models_catalog_read_never_blocks_the_event_loop(monkeypatch):
    import api.models as api_models
    import connections.harness_models as harness_models
    import connections.harness_refresh as harness_refresh
    import llm.capabilities as capabilities
    import llm.model_status as model_status

    def _slow_connection(*_args, **_kwargs):
        time.sleep(0.2)
        return None

    def _slow_rows():
        time.sleep(0.2)
        return [{"id": "model-1"}]

    event_loop_thread = threading.get_ident()

    snapshot = [{"thread_id": "thread-1", "model_id": "model-1"}]

    def _thread_snapshot():
        assert threading.get_ident() == event_loop_thread
        return snapshot

    def _active_threads(received):
        assert threading.get_ident() != event_loop_thread
        assert received == snapshot
        return []

    monkeypatch.setattr(harness_refresh, "schedule_stale_refresh", lambda: None)
    monkeypatch.setattr(harness_models, "get_connection", _slow_connection)
    monkeypatch.setattr(model_status, "get_model_status_rows", _slow_rows)
    monkeypatch.setattr(api_models, "snapshot_active_threads", _thread_snapshot)
    monkeypatch.setattr(capabilities, "get_active_local_threads", _active_threads)

    delay, result = await asyncio.gather(
        _heartbeat_delay(),
        api_models.list_models(),
    )

    assert delay < 0.1
    assert result["models"] == [{"id": "model-1"}]


@pytest.mark.asyncio
async def test_passive_models_refetch_schedules_stale_refresh_without_usage_rechecks(
    monkeypatch,
):
    import api.models as api_models
    import connections.harness_refresh as harness_refresh
    import connections.harness_usage_check as usage_checks
    import llm.model_catalog_projection as catalog_projection

    scheduled = []

    async def _schedule():
        scheduled.append(True)

    stale_refreshes: list[bool] = []

    # A real catalog refresh starts CLI subprocesses on hosts that have the
    # harnesses installed, and loop teardown then waits on them.
    monkeypatch.setattr(
        harness_refresh, "schedule_stale_refresh", lambda: stale_refreshes.append(True),
    )
    monkeypatch.setattr(usage_checks, "schedule_limited_usage_rechecks", _schedule)
    monkeypatch.setattr(
        catalog_projection, "model_catalog_projection", lambda _threads: {"models": []},
    )

    await api_models.list_models()
    assert scheduled == []
    await api_models.list_models(refresh_usage=True)
    assert scheduled == [True]
    assert stale_refreshes == [True, True]


@pytest.mark.asyncio
async def test_settings_snapshot_never_blocks_the_event_loop(monkeypatch):
    import api.system as api_system
    import core.workspace_context_sources as context_sources
    import infra.database as database
    import tools.browser as browser

    class _Db:
        def execute(self, *_args):
            return self

        def fetchone(self):
            return None

        def close(self):
            return None

    def _slow_flags():
        time.sleep(0.2)
        return {}

    monkeypatch.setattr(api_system, "get_feature_flags", _slow_flags)
    monkeypatch.setattr(api_system, "get_host_access_auto_approve", lambda: False)
    monkeypatch.setattr(api_system, "_host_access_reviewer_status", lambda: {})
    monkeypatch.setattr(context_sources, "workspace_context_files", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(database, "get_db", lambda: _Db())
    monkeypatch.setattr(browser, "browser_available", lambda: False)

    delay, result = await asyncio.gather(
        _heartbeat_delay(),
        api_system.get_settings({"id": "steve"}),
    )

    assert delay < 0.1
    assert "features" in result


@pytest.mark.asyncio
async def test_live_bulletin_settings_use_run_in_db(monkeypatch):
    import api.system as api_system

    calls = []

    async def fake_run_in_db(function, *args):
        calls.append((function, args))
        return {"enabled": True, "base_url": "https://feed.example/bulletins"}

    monkeypatch.setattr(api_system, "run_in_db", fake_run_in_db)
    result = await api_system.update_settings(
        {"live_bulletins": {"enabled": True, "base_url": "https://feed.example/bulletins/"}},
        user={"id": "steve", "token_kind": "session"},
    )

    assert calls == [
        (api_system._save_live_bulletin_settings, (True, "https://feed.example/bulletins/")),
    ]
    assert result["live_bulletins"] == {"enabled": True, "base_url": "https://feed.example/bulletins"}


@pytest.mark.asyncio
async def test_reviewer_model_save_uses_run_in_db(monkeypatch):
    import api.system as api_system

    calls = []
    body = {"host_access_reviewer_model": "codex/test"}

    async def fake_run_in_db(function, *args):
        calls.append((function, args))
        return {"host_access_reviewer_model": "codex/test"}

    monkeypatch.setattr(api_system, "run_in_db", fake_run_in_db)
    result = await api_system.update_settings(
        body, user={"id": "steve", "token_kind": "session"},
    )

    assert calls == [(api_system._save_exact_model_defaults, (body,))]
    assert result["host_access_reviewer_model"] == "codex/test"


@pytest.mark.asyncio
async def test_reviewer_fallback_save_uses_run_in_db(monkeypatch):
    import api.system as api_system

    calls = []

    async def fake_run_in_db(function, *args):
        calls.append((function, args))

    monkeypatch.setattr(api_system, "run_in_db", fake_run_in_db)
    result = await api_system.update_settings(
        {"host_access_reviewer_fallback": True},
        user={"id": "steve", "token_kind": "session"},
    )

    assert calls == [
        (api_system.save_agent_defaults, ({"host_access_reviewer_fallback": True},)),
    ]
    assert result["host_access_reviewer_fallback"] is True


@pytest.mark.asyncio
async def test_bulletin_tool_and_loop_use_the_db_worker(monkeypatch):
    import infra.bulletin_live as bulletin_live
    import infra.db_async as db_async
    from tools.bulletins import BulletinDispositionTool, _record_disposition

    loop_calls = []

    async def loop_run_in_db(function, *args):
        loop_calls.append((function, args))
        assert function is bulletin_live._prepare_check
        return None, None, None

    monkeypatch.setattr(bulletin_live, "run_in_db", loop_run_in_db)
    assert await bulletin_live.check_live_bulletins() is False
    assert loop_calls == [(bulletin_live._prepare_check, ())]

    tool_calls = []

    async def tool_run_in_db(function, *args):
        tool_calls.append((function, args))
        return '{"id": "bulletin", "state": "acted", "recorded": true}', None

    monkeypatch.setattr(db_async, "run_in_db", tool_run_in_db)
    assert await BulletinDispositionTool().execute("bulletin", "acted", "done", _thread_id="conductor") == (
        '{"id": "bulletin", "state": "acted", "recorded": true}'
    )
    assert tool_calls == [(_record_disposition, ("bulletin", "acted", "done", "conductor"))]


@pytest.mark.asyncio
async def test_version_git_identity_never_blocks_the_event_loop(monkeypatch):
    import api.version as api_version

    event_loop_thread = threading.get_ident()
    worker_started = threading.Event()
    worker_waiting = threading.Event()
    worker_release = threading.Event()
    outputs = {
        ("rev-parse", "--short=7", "HEAD"): "abc1234",
        ("rev-parse", "HEAD"): "abc1234" + "0" * 33,
        ("log", "-1", "--format=%cI", "HEAD"): "2026-09-10T15:00:00-07:00",
        ("log", "-1", "--format=%s", "HEAD"): "subject",
        ("status", "--porcelain"): " M backend/api/version.py",
    }

    def _slow_git(args):
        worker_started.set()
        assert threading.get_ident() != event_loop_thread
        worker_waiting.set()
        assert worker_release.wait(timeout=5), "test did not release the git worker"
        return outputs[tuple(args)]

    # _git_read is the one subprocess seam; _git and the dirty read both go through it.
    monkeypatch.setattr(api_version, "_git_read", _slow_git)

    version_task = asyncio.create_task(api_version.get_version())
    try:
        # Event ordering is the oracle; timeouts only bound a broken test.
        await asyncio.wait_for(asyncio.to_thread(worker_started.wait), timeout=5)
        if version_task.done():
            await version_task
        await asyncio.wait_for(asyncio.to_thread(worker_waiting.wait), timeout=5)
        assert not worker_release.is_set()
    finally:
        worker_release.set()
    result = await version_task

    assert result["backend"]["current_head"] == {
        "short_hash": "abc1234",
        "full_hash": "abc1234" + "0" * 33,
        "commit_iso": "2026-09-10T15:00:00-07:00",
        "commit_subject": "subject",
        "dirty": True,
    }


@pytest.mark.asyncio
async def test_provider_status_read_never_blocks_the_event_loop(monkeypatch):
    import api.provider_status as provider_status
    import llm.catalog as catalog
    import llm.network_discovery as discovery

    def _slow_servers(*_args, **_kwargs):
        time.sleep(0.2)
        return []

    monkeypatch.setattr(discovery, "get_servers", _slow_servers)
    monkeypatch.setattr(catalog, "list_models", lambda *_args, **_kwargs: [])

    delay, result = await asyncio.gather(
        _heartbeat_delay(),
        provider_status.build_provider_status("omlx"),
    )

    assert delay < 0.1
    assert result["server"]["servers"] == []


@pytest.mark.asyncio
async def test_mcp_secret_resolution_never_blocks_the_event_loop(monkeypatch):
    from contextlib import AsyncExitStack

    from tools import mcp, mcp_secrets

    event_loop_thread = threading.get_ident()
    worker_started = threading.Event()
    worker_release = threading.Event()

    def slow_resolve(*_args, **_kwargs):
        assert threading.get_ident() != event_loop_thread
        worker_started.set()
        assert worker_release.wait(timeout=5)
        return {}, {}

    async def worker(function, *args):
        return await asyncio.to_thread(function, *args)

    monkeypatch.setattr(mcp_secrets, "resolve", slow_resolve)
    monkeypatch.setattr(mcp, "run_in_db", worker)

    task = asyncio.create_task(mcp._open_session("server", {}, AsyncExitStack()))
    try:
        await asyncio.wait_for(asyncio.to_thread(worker_started.wait), timeout=5)
        assert await _heartbeat_delay() < 0.1
    finally:
        worker_release.set()
    with pytest.raises(mcp.MCPConfigError):
        await task
