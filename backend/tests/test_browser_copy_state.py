"""Reading the holder's sign-in for a copy: cookies, and each open tab's storage under the browser's own origin."""
import asyncio

import pytest

from host_access import browser_cdp, browser_copy_state, browser_session_launch
from host_access.browser_session_state import BrowserSessionError

_COOKIE = {"name": "sid", "value": "1", "domain": "a.test", "path": "/"}


class _Cdp:
    """A browser connection: its targets, and per-tab storage answers ('stuck', 'error' or {origin-kind: entries})."""

    def __init__(self, targets, tabs):
        self.targets, self.tabs, self.asked = targets, tabs, []

    def __call__(self, endpoint, *, max_size):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def call(self, method, **params):
        if method == "Storage.getCookies":
            return {"cookies": [_COOKIE, "not-a-cookie"]}
        if method == "Target.getTargets":
            return {"targetInfos": self.targets}
        assert method == "Target.attachToTarget"
        return {"sessionId": f"s-{params['targetId']}"}

    async def call_in(self, session, method, **params):
        assert method == "DOMStorage.getDOMStorageItems"
        storage_id = params["storageId"]
        self.asked.append((session, storage_id["securityOrigin"], storage_id["isLocalStorage"]))
        answer = self.tabs[session.removeprefix("s-")]
        if answer == "stuck":
            await asyncio.Event().wait()
        if answer == "error":
            raise BrowserSessionError("This page couldn't be checked")
        return {"entries": answer.get("local" if storage_id["isLocalStorage"] else "session", [])}


def _page(target_id, url, kind="page"):
    return {"targetId": target_id, "type": kind, "url": url}


@pytest.fixture
def cdp(monkeypatch):
    def install(targets, tabs):
        fake = _Cdp(targets, tabs)
        monkeypatch.setattr(browser_cdp, "CdpClient", fake)
        return fake
    return install


async def test_storage_is_read_under_the_origin_the_browser_reports_for_each_tab(cdp):
    fake = cdp([_page("a", "https://user@a.test:8443/app?x=1"), _page("b", "http://b.test/"),
                _page("w", "https://a.test/worker.js", kind="service_worker"), _page("c", "chrome://newtab/"),
                _page("e", "https://empty.test/")],
               {"a": {"local": [["token", "t"]], "session": [["tab", "1"]]}, "b": {"session": [["k", "v"]]},
                "e": {}})
    state = await browser_copy_state.read("ws://holder")
    assert state["cookies"] == [_COOKIE]
    assert state["origins"] == [
        {"origin": "https://a.test:8443", "localStorage": [{"name": "token", "value": "t"}],
         "sessionStorage": [{"name": "tab", "value": "1"}]},
        {"origin": "http://b.test", "localStorage": [], "sessionStorage": [{"name": "k", "value": "v"}]}]
    # Only web pages are asked, and only for their own origin; nothing runs in the page.
    assert {(session, origin) for session, origin, _ in fake.asked} == {
        ("s-a", "https://a.test:8443"), ("s-b", "http://b.test"), ("s-e", "https://empty.test")}


async def test_a_stuck_or_failing_tab_leaves_only_its_own_storage_out(cdp, monkeypatch, caplog):
    monkeypatch.setattr(browser_copy_state, "_PAGE_TIMEOUT", 0.05)
    cdp([_page("stuck", "https://stuck.test/"), _page("gone", "https://gone.test/"), _page("ok", "https://ok.test/")],
        {"stuck": "stuck", "gone": "error", "ok": {"local": [["k", "v"]]}})
    caplog.set_level("INFO", logger=browser_copy_state.__name__)
    state = await browser_copy_state.read("ws://holder")
    assert state["cookies"] == [_COOKIE]
    assert [entry["origin"] for entry in state["origins"]] == ["https://ok.test"]
    assert [record.getMessage() for record in caplog.records] == [
        "browser sign-in copy left out one page's storage: TimeoutError",
        "browser sign-in copy left out one page's storage: BrowserSessionError"]


async def test_many_stuck_tabs_end_inside_the_budget_and_keep_the_cookies(cdp, monkeypatch, caplog):
    monkeypatch.setattr(browser_copy_state, "_PAGE_TIMEOUT", 0.05)
    monkeypatch.setattr(browser_copy_state, "_PAGES_BUDGET", 0.12)
    cdp([_page(str(i), f"https://s{i}.test/") for i in range(10)], {str(i): "stuck" for i in range(10)})
    caplog.set_level("INFO", logger=browser_copy_state.__name__)
    state = await asyncio.wait_for(browser_copy_state.read("ws://holder"), 1.0)
    assert state == {"cookies": [_COOKIE], "origins": []}
    assert any("open pages: out of time" in record.getMessage() for record in caplog.records)


async def test_a_programming_error_reading_a_tab_is_not_swallowed(cdp, monkeypatch):
    cdp([_page("a", "https://a.test/")], {"a": {}})

    async def broken(*_args, **_kwargs):
        raise KeyError("entries")

    monkeypatch.setattr(browser_copy_state, "_items", broken)
    with pytest.raises(KeyError):
        await browser_copy_state.read("ws://holder")


def test_the_whole_read_fits_inside_the_copy_limit():
    worst = 2 * browser_cdp.BROWSER_CALL_TIMEOUT + browser_copy_state._PAGES_BUDGET
    assert worst < browser_session_launch._COPY_READ_TIMEOUT
