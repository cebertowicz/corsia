"""The clear's fence covers only browsers holding the site, and no fence read waits on file I/O."""
import json
import threading
from types import SimpleNamespace

import pytest

from host_access import browser_chrome, browser_grant_copy, browser_grant_records as records, browser_launch
from host_access import browser_session_launch as launch
from host_access.browser_session_state import BrowserSessionError
from tests.browser_process_fixture import synthetic_browser  # noqa: F401
from tests.test_browser_saved_signin import _saving_state, chrome154  # noqa: F401

COOKIE = {"name": "sid", "value": "one", "domain": ".example.com", "path": "/"}
OTHER = {"name": "keep", "value": "two", "domain": ".other.org", "path": "/"}


@pytest.fixture(autouse=True)
def clear_memory():
    for state in (records._states, records._grants, records._generation, records._fences):
        state.clear()
    yield
    for state in (records._states, records._grants, records._generation, records._fences):
        state.clear()


@pytest.mark.asyncio
async def test_fence_refuses_holder_and_copy_only(synthetic_browser, chrome154, monkeypatch):
    fake = synthetic_browser
    _saving_state(fake, monkeypatch)
    fixture_launch = browser_chrome.ChromeProcess.launch

    async def launch_with_pid(*args, **kwargs):  # A second saved profile reads the others' pids.
        chrome = await fixture_launch(*args, **kwargs)
        chrome.closed, chrome.process = False, SimpleNamespace(pid=40000 + len(fake.chrome_calls))
        return chrome
    monkeypatch.setattr(browser_chrome.ChromeProcess, "launch", launch_with_pid)
    monkeypatch.setattr("host_access.browser_orphan.close_orphans", lambda *_: None)
    owners = {"a": "fixture-owner", "b": "fixture-owner", "c": "other-owner"}
    monkeypatch.setattr(browser_launch, "_owner_key", lambda thread_id: owners.get(thread_id))
    for thread_id in ("a", "b", "c", "d"):
        await fake.owner.run(thread_id, ["open", "about:blank"])
    assert fake.owner._sessions["b"].record.copy_of == "a"
    profile = str(browser_launch.profile_dir("a"))
    records._raise_fence((profile, "example.com"))
    for thread_id in ("a", "b"):
        with pytest.raises(BrowserSessionError, match="^browser session unavailable$"):
            await fake.owner.run(thread_id, ["get", "url"])
        await fake.owner.run(thread_id, ["get", "url"], pane=True)  # The person's pane is not fenced.
    await fake.owner.run("c", ["get", "url"])  # another profile
    await fake.owner.run("d", ["get", "url"])  # an ephemeral session
    records.release(profile, "example.com")
    await fake.owner.run("a", ["get", "url"])
    await fake.owner.shutdown()


def _blocked_write(target, entered, release, only):
    def write(path, data):
        if path == only:
            entered.set()
            assert release.wait(10)
        target(path, data)
    return write


@pytest.mark.parametrize("kind", ["copy", "kept"])
def test_write_in_flight_holds_no_fence_or_record_lock_and_is_filtered_by_a_concurrent_end(tmp_path, monkeypatch, kind):
    profile = str(tmp_path / "profile")
    records.arm(profile, "https://example.com")
    entered, release = threading.Event(), threading.Event()
    if kind == "copy":
        path = tmp_path / "copy.json"
        monkeypatch.setattr(launch, "_write_private_json", _blocked_write(launch._write_private_json, entered, release, path))
        work = threading.Thread(target=browser_grant_copy.write_state,
                                args=(profile, path, {"cookies": [COOKIE, OTHER], "origins": []}))
    else:
        path = launch.kept_signins_path(profile)
        monkeypatch.setattr(launch, "_write_cookies", _blocked_write(launch._write_cookies, entered, release, path))
        work = threading.Thread(target=browser_grant_copy.merge, args=(profile, [COOKIE, OTHER]))
    work.start()
    try:
        assert entered.wait(10)
        assert not records.fenced(profile)  # The event loop's read takes no lock a write holds.
        records.end(profile, "https://example.com")  # Not blocked by the write in flight.
        assert records.fenced(profile)
    finally:
        release.set()
        work.join(10)
    assert not work.is_alive()
    assert json.loads(path.read_text())["cookies"] == [OTHER]


def test_fence_read_does_not_wait_on_the_record_lock(tmp_path):
    profile = str(tmp_path / "profile")
    records.end(profile, "https://example.com")
    held, done = threading.Event(), threading.Event()

    def hold():
        with records._records_lock:
            held.set()
            done.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert held.wait(10)
        assert records.fenced(profile)
        assert not records.fenced(str(tmp_path / "other"))
    finally:
        done.set()
        holder.join(10)
