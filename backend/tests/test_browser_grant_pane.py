"""A pane opened first cannot receive a frame before grant recovery."""
from types import SimpleNamespace

import pytest

from host_access import browser_grant_cleanup, browser_grant_records, browser_launch, browser_pane
from tests.browser_process_fixture import synthetic_browser  # noqa: F401
from tests.test_browser_saved_signin import chrome154  # noqa: F401


@pytest.mark.asyncio
async def test_pane_first_recovers_standing_site_before_frame(synthetic_browser, chrome154, monkeypatch):
    fake = synthetic_browser
    profile = str(browser_launch.profile_dir("a"))
    browser_grant_records.arm(profile, "https://example.com")
    monkeypatch.setattr(browser_grant_records, "_states", {})  # Restart loses in-memory state.
    monkeypatch.setattr(browser_grant_records, "_grants", {})
    events = []

    async def clear(_endpoint, site):
        assert not fake.calls
        events.append(("clear", site))

    async def page_endpoint(_command):
        assert not browser_grant_records.pending(profile)
        return "ws://page"

    class Cast:
        def __init__(self, _endpoint, _deliver):
            pass

        async def run(self):
            events.append(("frame", None))

    monkeypatch.setattr(browser_grant_cleanup.browser_site_clear, "clear", clear)
    monkeypatch.setattr(browser_pane, "PROCESS_OWNER", SimpleNamespace(browser_sessions=lambda: fake.owner))
    monkeypatch.setattr(browser_pane.browser_cdp, "page_endpoint", page_endpoint)
    monkeypatch.setattr(browser_pane.browser_screencast, "PaneScreencast", Cast)
    monkeypatch.setattr(browser_pane.browser_pane_state, "screencast_started", lambda *_args: None)
    monkeypatch.setattr(browser_pane.browser_pane_state, "screencast_ended", lambda *_args: None)
    try:
        await browser_pane.open_url("a", "about:blank")
        await browser_pane.screencast("a", "test", lambda _frame: None)
        assert events == [("clear", "https://example.com"), ("frame", None)]
    finally:
        browser_pane.clear_page_endpoint("a", "test")
        await fake.owner.shutdown()
