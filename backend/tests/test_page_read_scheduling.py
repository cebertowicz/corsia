"""Exercise page reads against occupied DB executors and real lane behavior."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from loguru import logger

from infra.db_connection import mark_thread_lane


async def _started(event):
    for _ in range(500):
        if event.is_set():
            return
        await asyncio.sleep(0.01)
    pytest.fail("worker did not start")


@pytest.mark.asyncio
async def test_status_and_message_search_bypass_occupied_writer(isolated_db, monkeypatch):
    from api.system_admin import system_update_status
    from infra import db_async, update_status
    from tools.query_search import SearchHistoryTool

    monkeypatch.setattr(update_status, "update_status", lambda _: SimpleNamespace(to_dict=lambda: {"available": False}))
    started, release = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=1) as writer:
        monkeypatch.setattr(db_async, "_pool", writer)
        def hold():
            started.set()
            release.wait(5)
        holder = asyncio.create_task(db_async.run_in_db(hold))
        try:
            await _started(started)
            status = await asyncio.wait_for(system_update_status(user={}), 3)
            assert status["available"] is False
            assert status["usual_seconds"] is None
            assert status["agents_working"] == 0
            assert status["schedule"] == {"automatic": False, "time": "03:00", "tonight_at": None}
            assert "operation" not in status
            result = await asyncio.wait_for(SearchHistoryTool().execute("missing"), 3)
            assert result == "No messages found matching 'missing'"
            from api.system_admin import system_update_outcome_seen
            write = asyncio.create_task(system_update_outcome_seen("missing", user={}))
            await asyncio.sleep(0)
            assert not write.done()
            release.set()
            from fastapi import HTTPException
            with pytest.raises(HTTPException) as error:
                await asyncio.wait_for(write, 3)
            assert (error.value.status_code, error.value.detail) == (404, "No settled Update with that id.")
        finally:
            release.set()
            await holder


@pytest.mark.asyncio
async def test_status_group_preserves_order_error_and_event_loop_progress(isolated_db, monkeypatch):
    from api.system_admin import system_update_status
    from automation import self_restart
    from core import turn_core
    from infra import settings, update_status

    monkeypatch.setattr(update_status, "update_status", lambda _: SimpleNamespace(to_dict=lambda: {"available": None, "error": "offline"}))
    started, release = threading.Event(), threading.Event()
    calls = []
    def usual():
        calls.append("usual")
        started.set()
        release.wait(5)
        return 12
    def turns():
        calls.append("turns")
        return 2
    def schedule():
        calls.append("schedule")
        raise ValueError("schedule failed")
    monkeypatch.setattr(self_restart, "usual_update_seconds", usual)
    monkeypatch.setattr(turn_core, "active_turn_count", turns)
    monkeypatch.setattr(settings, "get_update_schedule", schedule)
    monkeypatch.setattr(self_restart, "latest_update_operation", lambda: pytest.fail("must not read after error"))
    task = asyncio.create_task(system_update_status(user={}))
    try:
        await _started(started)
        assert not task.done()
        release.set()
        with pytest.raises(ValueError, match="schedule failed"):
            await asyncio.wait_for(task, 3)
        assert calls == ["usual", "turns", "schedule"]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_queued_message_search_is_withdrawn(isolated_db, monkeypatch):
    from infra import db_async
    from tools import query_search

    started, release, searched = threading.Event(), threading.Event(), threading.Event()
    original = query_search.search_messages_fts
    def search(*args, **kwargs):
        searched.set()
        return original(*args, **kwargs)
    monkeypatch.setattr(query_search, "search_messages_fts", search)
    with ThreadPoolExecutor(max_workers=1, initializer=mark_thread_lane, initargs=("read",)) as pool:
        monkeypatch.setattr(db_async, "_read_pool", pool)
        def hold():
            started.set()
            release.wait(5)
        holder = asyncio.create_task(db_async.run_read(hold))
        try:
            await _started(started)
            task = asyncio.create_task(query_search.SearchHistoryTool().execute("missing"))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
            release.set()
            await holder
            await db_async.run_read(lambda: None)
            assert not searched.is_set()
        finally:
            release.set()
            await asyncio.gather(holder, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_queued_checkpoint_search_completes_and_logs(isolated_db, monkeypatch):
    from infra import db_async
    from tools.query_search import SearchHistoryTool

    started, release = threading.Event(), threading.Event()
    records = []
    sink = logger.add(records.append, format="{message}")
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            monkeypatch.setattr(db_async, "_pool", pool)
            def hold():
                started.set()
                release.wait(5)
            holder = asyncio.create_task(db_async.run_in_db(hold))
            try:
                await _started(started)
                task = asyncio.create_task(SearchHistoryTool().execute("missing", include_checkpoints=True, _thread_id="nonexistent"))
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 3)
                assert any("checkpoint_search reason=unresolved_caller" in str(record) for record in records)
            finally:
                release.set()
                await asyncio.gather(holder, return_exceptions=True)
    finally:
        logger.remove(sink)
