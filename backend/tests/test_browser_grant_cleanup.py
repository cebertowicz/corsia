"""Pending-site cleanup fences browser actions and retains incomplete work."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from host_access import browser_grant_cleanup as cleanup, browser_grant_copy, browser_grant_records as records
from host_access import browser_session_launch as launch


@pytest.fixture(autouse=True)
def reset_records():
    for rows in (records._armed, records._pending, records._generation, records._transitions):
        rows.clear()
    yield
    for rows in (records._armed, records._pending, records._generation, records._transitions):
        rows.clear()


def runtime(profile):
    return SimpleNamespace(record=SimpleNamespace(profile=profile, copy_of=None, thread_id="thread"),
                           action_lock=asyncio.Lock(), command_lock=asyncio.Lock(), attempt=None,
                           chrome=SimpleNamespace(endpoint="ws://test", closed=False))


def owner(item):
    result = SimpleNamespace(_owner_lock=asyncio.Lock(), _grant_cleanup_lock=asyncio.Lock(),
                             _pending_sites=set(), _sessions={"thread": item})
    result._close_one = AsyncMock(return_value=None)
    return result


@pytest.mark.asyncio
async def test_running_clear_fences_then_removes_record(tmp_path, monkeypatch):
    profile = str(tmp_path / "profile")
    records.end(profile, "https://example.com")
    item = runtime(profile)
    parent = owner(item)
    async def clear(_endpoint, _site):
        assert parent._pending_sites
        assert item.action_lock.locked() and item.command_lock.locked()
    monkeypatch.setattr(cleanup.browser_site_clear, "clear", clear)
    await cleanup.clear_running(parent, profile, "example.com")
    assert not parent._pending_sites
    assert not records.pending(profile)


@pytest.mark.asyncio
async def test_failed_clear_closes_and_keeps_journal(tmp_path, monkeypatch):
    profile = str(tmp_path / "profile")
    records.end(profile, "https://example.com")
    item = runtime(profile)
    parent = owner(item)
    monkeypatch.setattr(cleanup.browser_site_clear, "clear", AsyncMock(side_effect=ConnectionError()))
    await cleanup.clear_running(parent, profile, "example.com")
    parent._close_one.assert_awaited_once()
    assert "example.com" in records.pending(profile)


@pytest.mark.asyncio
async def test_oversized_cookie_reply_retains_record_and_fence_when_close_unconfirmed(tmp_path, monkeypatch):
    profile = str(tmp_path / "profile")
    records.end(profile, "https://example.com")
    item = runtime(profile)
    item.chrome.close = AsyncMock(side_effect=RuntimeError("unconfirmed"))
    parent = owner(item)
    parent._close_one = AsyncMock(side_effect=RuntimeError("unconfirmed"))
    limits = []
    calls = []

    class OversizedCdp:
        def __init__(self, _endpoint, *, max_size):
            limits.append(max_size)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def call_in(self, _session, method, **_params):
            calls.append(method)
            raise ConnectionClosedError(Close(1009, "message too big"), None)

    monkeypatch.setattr(cleanup.browser_site_clear.browser_cdp, "CdpClient", OversizedCdp)
    await cleanup.clear_running(parent, profile, "example.com")
    assert limits == [32 * 1024 * 1024]
    assert calls == ["Storage.getCookies"]
    parent._close_one.assert_awaited_once()
    item.chrome.close.assert_awaited_once()
    assert "example.com" in records.pending(profile)
    assert (profile, "example.com") in parent._pending_sites


@pytest.mark.asyncio
async def test_busy_action_times_out_closes_and_keeps_record(tmp_path, monkeypatch):
    profile = str(tmp_path / "profile")
    records.end(profile, "https://example.com")
    item = runtime(profile)
    parent = owner(item)
    monkeypatch.setattr(cleanup, "COMMAND_TIMEOUT", 0.01)
    await item.action_lock.acquire()
    try:
        await cleanup.clear_running(parent, profile, "example.com")
    finally:
        item.action_lock.release()
    parent._close_one.assert_awaited_once()
    assert "example.com" in records.pending(profile)


@pytest.mark.asyncio
async def test_failed_close_terminates_chrome_and_keeps_record(tmp_path, monkeypatch):
    profile = str(tmp_path / "profile")
    records.end(profile, "https://example.com")
    item = runtime(profile)
    item.chrome.close = AsyncMock()
    parent = owner(item)
    parent._close_one = AsyncMock(side_effect=[RuntimeError("unverified"), None])
    monkeypatch.setattr(cleanup.browser_site_clear, "clear", AsyncMock(side_effect=ConnectionError()))
    await cleanup.clear_running(parent, profile, "example.com")
    item.chrome.close.assert_awaited_once()
    assert parent._close_one.await_count == 2
    assert "example.com" in records.pending(profile)


@pytest.mark.asyncio
async def test_launch_clears_before_return_and_failure_keeps_record(tmp_path, monkeypatch):
    profile = str(tmp_path / "profile")
    records.end(profile, "https://example.com")
    item = runtime(profile)
    call = AsyncMock(side_effect=ConnectionError())
    monkeypatch.setattr(cleanup.browser_site_clear, "clear", call)
    with pytest.raises(cleanup.PendingClearError):
        await cleanup.recover_launch(item)
    assert "example.com" in records.pending(profile)
    call.side_effect = None
    await cleanup.recover_launch(item)
    assert not records.pending(profile)


def test_corrupt_kept_file_is_preserved(tmp_path):
    profile = str(tmp_path / "profile")
    path = launch.kept_signins_path(profile)
    path.write_text("invalid")
    records.end(profile, "https://example.com")
    with pytest.raises(ValueError):
        browser_grant_copy.merge(profile, [])
    assert path.read_text() == "invalid"


def test_copy_writeback_filters_pending_site(tmp_path):
    profile = str(tmp_path / "profile")
    first = {"name": "a", "value": "one", "domain": ".example.com", "path": "/"}
    second = {"name": "b", "value": "two", "domain": ".other.org", "path": "/"}
    records.end(profile, "https://example.com")
    browser_grant_copy.merge(profile, [first, second])
    assert launch._read_cookies(launch.kept_signins_path(profile)) == [second]
