"""Observed navigation origins survive through site cleanup."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from host_access import browser_grant_cleanup, browser_grant_records as records, browser_launch, browser_page_watch, browser_pane, browser_site_clear


@pytest.fixture(autouse=True)
def clear_grants():
    for state in (records._states, records._grants, records._generation, records._fences):
        state.clear()
    yield
    for state in (records._states, records._grants, records._generation, records._fences):
        state.clear()


@pytest.mark.asyncio
async def test_pane_navigation_records_and_forwards_even_when_write_fails(tmp_path, monkeypatch):
    profile = str(tmp_path / "profile")
    records.arm(profile, "https://example.com")
    monkeypatch.setattr(browser_launch, "profile_dir", lambda _thread: profile)
    monkeypatch.setattr(browser_pane, "_page_endpoint", AsyncMock(return_value="ws://test"))

    addresses = ["https://sub.example.com:8443/path", "https://elsewhere.org/"]

    async def watch(_endpoint, report):
        for address in addresses:
            await report({"url": address})

    monkeypatch.setattr(browser_pane.browser_cdp, "watch_page_metadata", watch)
    delivered = AsyncMock()
    await browser_pane.watch_page_metadata("thread", "token", delivered)
    assert records.origins(profile, "example.com") == ["https://sub.example.com:8443"]
    assert delivered.await_count == 2
    monkeypatch.setattr(records, "_write", lambda *_args: (_ for _ in ()).throw(OSError()))
    addresses[0] = "https://new.example.com/private"
    await browser_pane.watch_page_metadata("thread", "token", delivered)
    assert delivered.await_count == 4


@pytest.mark.asyncio
async def test_page_watch_reports_navigation_without_changing_loader_marks(tmp_path, monkeypatch):
    profile = str(tmp_path / "profile")
    records.arm(profile, "https://example.com")
    monkeypatch.setattr(browser_launch, "profile_dir", lambda _thread: profile)
    seen = []
    watch = browser_page_watch.PageWatch(seen.append, 1, on_url=lambda url: records.observe_thread("thread", url))
    event = {"method": "Page.frameNavigated", "params": {"frame": {"loaderId": "L1", "url": "https://sub.example.com:8443/path"}}}
    answer = {"id": 7}
    messages = iter((json.dumps(event), json.dumps(answer)))
    cdp = SimpleNamespace(socket=SimpleNamespace(recv=AsyncMock(side_effect=lambda: next(messages))))
    watch._answer = 7
    assert await watch._listen(cdp) == answer
    assert seen == ["L1"]
    assert records.origins(profile, "example.com") == ["https://sub.example.com:8443"]


@pytest.mark.asyncio
async def test_recovery_passes_origins_and_old_records_still_clear(tmp_path, monkeypatch):
    profile = str(tmp_path / "profile")
    records.arm(profile, "https://example.com")
    records.observe(profile, "https://closed.example.com:8443/page")
    records._states.clear()  # A restart keeps the record and loses every in-memory state.
    records._grants.clear()
    runtime = SimpleNamespace(record=SimpleNamespace(profile=profile), chrome=SimpleNamespace(endpoint="ws://test"))
    clear = AsyncMock()
    monkeypatch.setattr(browser_site_clear, "clear", clear)
    await browser_grant_cleanup.recover_launch(runtime)
    clear.assert_awaited_once_with("ws://test", "https://example.com", recorded_origins=("https://closed.example.com:8443",))
    records._path(profile).write_text(json.dumps({"sites": ["https://example.com"]}))
    clear.reset_mock()
    await browser_grant_cleanup.recover_launch(runtime)
    clear.assert_awaited_once_with("ws://test", "https://example.com")
