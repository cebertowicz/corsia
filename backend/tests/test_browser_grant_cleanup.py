"""A site clear fences agents on its holders, retains incomplete work, and fails closed on any error."""
import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from host_access import browser_grant_cleanup as cleanup, browser_grant_copy, browser_grant_records as records
from host_access import browser_session_launch as launch


@pytest.fixture(autouse=True)
def reset_records(monkeypatch):
    for rows in (records._states, records._grants, records._generation, records._fences):
        rows.clear()
    events = []
    monkeypatch.setattr(cleanup, "emit_product_event", lambda *args, **kwargs: events.append((args, kwargs)))
    yield events
    for rows in (records._states, records._grants, records._generation, records._fences):
        rows.clear()


def outcomes(events):
    assert all(args == ("browser_signout", "site_clear") and set(kwargs) == {"properties"}
               and set(kwargs["properties"]) == {"outcome", "cause"} for args, kwargs in events)
    return [kwargs["properties"]["outcome"] for _args, kwargs in events]


def runtime(profile):
    return SimpleNamespace(record=SimpleNamespace(profile=profile, site_profile=profile, copy_of=None, thread_id="thread"),
                           action_lock=asyncio.Lock(), command_lock=asyncio.Lock(), attempt=None,
                           chrome=SimpleNamespace(endpoint="ws://test", closed=False))


def owner(item):
    result = SimpleNamespace(_owner_lock=asyncio.Lock(), _grant_cleanup_lock=asyncio.Lock(), _sessions={"thread": item})
    result._close_one = AsyncMock(return_value=None)
    return result


@pytest.mark.asyncio
async def test_running_clear_fences_then_removes_record(tmp_path, monkeypatch, reset_records):
    profile = str(tmp_path / "profile")
    records.end(profile, "https://example.com")
    item = runtime(profile)
    parent = owner(item)
    async def clear(_endpoint, _site):
        assert records.fenced(profile)
        assert item.action_lock.locked() and item.command_lock.locked()
    monkeypatch.setattr(cleanup.browser_site_clear, "clear", clear)
    await cleanup.clear_running(parent, profile, "example.com", "revoked")
    assert not records.fenced(profile)
    assert not records.pending(profile)
    assert reset_records == [(("browser_signout", "site_clear"), {"properties": {"outcome": "cleared", "cause": "revoked"}})]


@pytest.mark.asyncio
async def test_failed_clear_closes_and_keeps_journal(tmp_path, monkeypatch, reset_records):
    profile = str(tmp_path / "profile")
    records.end(profile, "https://example.com")
    item = runtime(profile)
    parent = owner(item)
    monkeypatch.setattr(cleanup.browser_site_clear, "clear", AsyncMock(side_effect=ConnectionError()))
    await cleanup.clear_running(parent, profile, "example.com", "expired")
    parent._close_one.assert_awaited_once()
    assert "example.com" in records.pending(profile)
    assert not records.fenced(profile)
    assert outcomes(reset_records) == ["clear_failed", "browser_closed"]


@pytest.mark.asyncio
async def test_oversized_cookie_reply_retains_record_and_fence_when_close_unconfirmed(tmp_path, monkeypatch, reset_records):
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
    await cleanup.clear_running(parent, profile, "example.com", "revoked")
    assert limits == [32 * 1024 * 1024]
    assert calls == ["Storage.getCookies"]
    parent._close_one.assert_awaited_once()
    item.chrome.close.assert_awaited_once()
    assert "example.com" in records.pending(profile)
    assert records.fenced(profile)
    assert outcomes(reset_records) == ["clear_failed"]


@pytest.mark.asyncio
async def test_busy_action_times_out_closes_and_keeps_record(tmp_path, monkeypatch):
    profile = str(tmp_path / "profile")
    records.end(profile, "https://example.com")
    item = runtime(profile)
    parent = owner(item)
    monkeypatch.setattr(cleanup, "COMMAND_TIMEOUT", 0.01)
    await item.action_lock.acquire()
    try:
        await cleanup.clear_running(parent, profile, "example.com", "revoked")
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
    await cleanup.clear_running(parent, profile, "example.com", "revoked")
    item.chrome.close.assert_awaited_once()
    assert parent._close_one.await_count == 2
    assert "example.com" in records.pending(profile)
    assert not records.fenced(profile)


@pytest.mark.parametrize("terminated", [True, False])
@pytest.mark.asyncio
async def test_unexpected_error_takes_the_failure_path(tmp_path, monkeypatch, caplog, reset_records, terminated):
    profile = str(tmp_path / "profile")
    records.end(profile, "https://example.com")
    item = runtime(profile)
    item.chrome.close = AsyncMock(side_effect=None if terminated else RuntimeError("stuck"))
    parent = owner(item)
    monkeypatch.setattr(records, "origins", lambda *_args: (_ for _ in ()).throw(ValueError("invalid journal")))
    parent._close_one = AsyncMock(side_effect=[RuntimeError("unverified"), None])
    await cleanup.clear_running(parent, profile, "example.com", "revoked")
    item.chrome.close.assert_awaited_once()
    assert "example.com" in records.pending(profile)
    assert records.fenced(profile) is not terminated
    assert "site clear failed for example.com (ValueError)" in caplog.text
    assert outcomes(reset_records) == ["clear_failed", "browser_closed"] if terminated else ["clear_failed"]


@pytest.mark.asyncio
async def test_cancel_inside_close_keeps_fence_and_pending_record(tmp_path, monkeypatch):
    profile = str(tmp_path / "profile")
    records.end(profile, "https://example.com")
    item = runtime(profile)
    parent = owner(item)
    closing = asyncio.Event()

    async def close_one(_runtime):
        closing.set()
        await asyncio.Event().wait()

    parent._close_one = close_one
    monkeypatch.setattr(cleanup.browser_site_clear, "clear", AsyncMock(side_effect=ConnectionError()))
    task = asyncio.create_task(cleanup.clear_running(parent, profile, "example.com", "revoked"))
    await closing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert records.fenced(profile)
    assert records._states[(profile, "example.com")] is records.SiteState.PENDING
    assert "example.com" in records.pending(profile)


@pytest.mark.asyncio
async def test_no_running_holder_defers_and_a_newer_end_supersedes(tmp_path, monkeypatch, reset_records):
    profile = str(tmp_path / "profile")
    records.end(profile, "https://example.com")
    copy = runtime(profile)
    copy.record.profile, copy.chrome = None, None
    parent = owner(copy)
    monkeypatch.setattr(launch, "drop_copy", lambda _record: None)
    await cleanup.clear_running(parent, profile, "example.com", "expired")
    assert "example.com" in records.pending(profile) and not records.fenced(profile)
    item = runtime(profile)
    parent = owner(item)

    async def clear(_endpoint, _site):
        records.end(profile, "https://example.com")
    monkeypatch.setattr(cleanup.browser_site_clear, "clear", clear)
    await cleanup.clear_running(parent, profile, "example.com", "revoked")
    assert "example.com" in records.pending(profile)
    assert outcomes(reset_records) == ["deferred", "superseded"]


@pytest.mark.asyncio
async def test_launch_clears_before_return_and_failure_keeps_record(tmp_path, monkeypatch, reset_records):
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
    assert outcomes(reset_records) == ["clear_failed", "cleared"]
    assert {kwargs["properties"]["cause"] for _args, kwargs in reset_records} == {cleanup.LAUNCH}


def test_corrupt_kept_file_is_preserved(tmp_path, caplog):
    profile = str(tmp_path / "profile")
    path = launch.kept_signins_path(profile)
    path.write_text("invalid")
    records.end(profile, "https://example.com")
    with pytest.raises(ValueError):
        browser_grant_copy.merge(profile, [])
    assert browser_grant_copy.kept(profile) == []
    browser_grant_copy.drop(profile, "example.com")
    assert path.read_text() == "invalid"
    assert "kept browser sign-ins unreadable" in caplog.text


@pytest.mark.asyncio
async def test_corrupt_kept_file_does_not_fail_launch_and_the_clear_still_runs(tmp_path, monkeypatch):
    profile = str(tmp_path / "profile")
    path = launch.kept_signins_path(profile)
    path.write_text("invalid")
    records.end(profile, "https://example.com")
    item = runtime(profile)
    clear = AsyncMock()
    monkeypatch.setattr(cleanup.browser_site_clear, "clear", clear)
    await cleanup.recover_launch(item)
    await launch.apply_kept_signins(item)
    clear.assert_awaited_once_with("ws://test", "https://example.com")
    assert not records.pending(profile)
    assert path.read_text() == "invalid"


def test_copy_writeback_filters_pending_site(tmp_path):
    profile = str(tmp_path / "profile")
    first = {"name": "a", "value": "one", "domain": ".example.com", "path": "/"}
    second = {"name": "b", "value": "two", "domain": ".other.org", "path": "/"}
    records.end(profile, "https://example.com")
    browser_grant_copy.merge(profile, [first, second])
    assert launch._read_cookies(launch.kept_signins_path(profile)) == [second]


SID = {"name": "sid", "value": "one", "domain": ".example.com", "path": "/"}


def _copy_and_holder(tmp_path, monkeypatch, calls, cleared=None):
    profile = str(tmp_path / "profile")
    holder = runtime(profile)
    holder.record.terminal_state = "running"
    copy = runtime(profile)
    copy.record = SimpleNamespace(profile=None, site_profile=profile, copy_of="thread", thread_id="copy",
                                  session="s-copy", terminal_state="resetting")
    copy.chrome = SimpleNamespace(endpoint="ws://copy", closed=False)
    parent = owner(holder)
    parent._sessions["copy"] = copy

    class Cdp:
        def __init__(self, endpoint, **_kwargs):
            self.endpoint = endpoint

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def call(self, method, **params):
            if method == "Storage.getCookies":
                return {"cookies": [SID]}
            if cleared is not None:  # A clear not held off by the holder's lock runs to its end here.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(cleared.wait(), 1)
            calls.append(("set", self.endpoint, [cookie["domain"] for cookie in params["cookies"]]))
            return {}
    monkeypatch.setattr(launch.browser_cdp, "CdpClient", Cdp)
    monkeypatch.setattr(launch, "resolve_profile", AsyncMock(return_value=(profile, None)))
    return profile, holder, copy, parent


@pytest.mark.asyncio
async def test_a_clear_started_after_kept_is_read_waits_until_set_cookies_ran(tmp_path, monkeypatch, reset_records):
    calls, cleared = [], asyncio.Event()
    profile, _holder, copy, parent = _copy_and_holder(tmp_path, monkeypatch, calls, cleared)
    loop = asyncio.get_running_loop()
    clears = []
    original = browser_grant_copy.kept

    def kept_then_end(path):
        rows = original(path)  # Read before the end: the site is still in it.
        records.end(profile, "https://example.com")
        loop.call_soon_threadsafe(lambda: clears.append(
            asyncio.ensure_future(cleanup.clear_running(parent, profile, "example.com", "revoked"))))
        return rows

    async def clear(endpoint, _site):
        calls.append(("clear", endpoint))
        cleared.set()
    monkeypatch.setattr(browser_grant_copy, "kept", kept_then_end)
    monkeypatch.setattr(cleanup.browser_site_clear, "clear", clear)
    async with copy.command_lock:  # As _close_runtime holds it.
        await launch.keep_copy_signins(parent, copy)
    await asyncio.gather(*clears)
    assert calls == [("set", "ws://test", [".example.com"]), ("clear", "ws://test"), ("clear", "ws://copy")]
    assert launch._read_cookies(launch.kept_signins_path(profile)) == []
    assert not records.pending(profile) and not records.fenced(profile)


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", ["timeout", "closed_while_waiting"])
async def test_a_busy_or_closing_holder_leaves_kept_sign_ins_for_the_next_launch(tmp_path, monkeypatch, caplog, busy):
    calls = []
    profile, holder, copy, parent = _copy_and_holder(tmp_path, monkeypatch, calls)
    monkeypatch.setattr(cleanup, "COMMAND_TIMEOUT", 0.05 if busy == "timeout" else 5)
    await holder.command_lock.acquire()
    closing = None
    if busy == "closed_while_waiting":
        async def close_holder():
            await asyncio.sleep(0.05)
            holder.record.terminal_state = "resetting"
            holder.command_lock.release()
        closing = asyncio.ensure_future(close_holder())
    await launch.keep_copy_signins(parent, copy)
    if closing is not None:
        await closing
    else:
        holder.command_lock.release()
        assert "kept browser sign-ins wait for the next launch (TimeoutError)" in caplog.text
    assert calls == []
    assert launch._read_cookies(launch.kept_signins_path(profile)) == [SID]
