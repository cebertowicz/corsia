"""The shared saved sign-in profile, its concurrent copies, the agent-route refusals and headless stealth."""
from pathlib import Path
import sqlite3
import sys

import pytest
from infra.config import STATE_ROOT
from host_access import browser_cli, browser_launch, browser_session_launch, browser_sessions as sessions
from tests.browser_process_fixture import synthetic_browser  # noqa: F401
from tools.host_path_policy import ProtectedHostPathPolicy

_REAL_OWNER_KEY = browser_launch._owner_key


@pytest.fixture
def chrome154(monkeypatch, tmp_path):
    chrome = tmp_path / "chrome"
    chrome.touch()
    monkeypatch.setattr(browser_launch, "chrome_major", lambda chrome: "154")
    monkeypatch.setattr(browser_cli, "chrome_executable", lambda: (str(chrome), "system"))
    monkeypatch.setattr(browser_launch, "_owner_key", lambda thread_id: "fixture-owner")


def _launches(fake):
    """Each browser command, minus the owner's own sign-in copy requests."""
    return [call for call in fake.calls if "state" not in call]


def _value(call, flag):
    return call[call.index(flag) + 1]


def _saving_state(fake, monkeypatch):
    """A `state save` that writes the file agent-browser would, so the next session can start from it."""
    original = fake.invoke

    async def invoke(argv, *, timeout):
        if "state" in argv and "save" in argv:
            Path(argv[-1]).write_text("{}")
        return await original(argv, timeout=timeout)

    monkeypatch.setattr(fake.owner, "_invoke", invoke)


def test_the_profile_is_one_per_owning_user_under_the_state_root(monkeypatch):
    monkeypatch.setattr(browser_launch, "_owner_key", lambda thread_id: {"a": "user-1", "b": "user-1", "c": "user-2"}[thread_id])
    assert browser_launch.profile_dir("a") == browser_launch.profile_dir("b")
    assert browser_launch.profile_dir("a") != browser_launch.profile_dir("c")
    assert browser_launch.profile_dir("a").parent == browser_launch.profile_root()
    assert browser_launch.profile_root().parent == STATE_ROOT
    # Never a profile of the person's own Chrome.
    assert "Google/Chrome" not in str(browser_launch.profile_dir("a"))


def test_an_unreadable_owner_gets_no_saved_profile(synthetic_browser):
    assert browser_launch.profile_dir("no-such-thread") is None


async def test_the_first_session_launches_on_the_shared_profile_and_stops_announcing_automation(synthetic_browser, chrome154):
    fake = synthetic_browser
    await fake.owner.run("a", ["open", "about:blank"])
    await fake.owner.run("a", ["snapshot"])
    first, later = _launches(fake)
    args = fake.chrome_calls[0]
    assert f"--user-data-dir={browser_launch.profile_dir('a')}" in args
    assert "--disable-blink-features=AutomationControlled" in args
    ua = next(arg for arg in args if arg.startswith("--user-agent=")).removeprefix("--user-agent=")
    assert ua.endswith("Chrome/154.0.0.0 Safari/537.36") and "HeadlessChrome" not in ua
    assert Path(_value(first, "--init-script")).is_file()
    # Changing launch settings on a later command makes agent-browser switch browsers and lose the page.
    for flag in ("--cdp", "--init-script"):
        assert _value(later, flag) == _value(first, flag)
    assert len(fake.chrome_calls) == 1
    await fake.owner.shutdown()


async def test_a_visible_browser_keeps_chromes_own_user_agent(synthetic_browser, chrome154):
    fake = synthetic_browser
    await fake.owner.run("a", ["open", "about:blank"], show_window=True)
    args = fake.chrome_calls[0]
    assert not any(arg.startswith("--user-agent=") for arg in args)
    assert "--disable-blink-features=AutomationControlled" in args
    await fake.owner.shutdown()


async def test_the_environments_own_launch_args_survive(synthetic_browser, chrome154, monkeypatch):
    monkeypatch.setenv("AGENT_BROWSER_ARGS", "--no-sandbox\n--disable-extensions")
    await synthetic_browser.owner.run("a", ["open", "about:blank"])
    args = synthetic_browser.chrome_calls[0]
    assert ("--no-sandbox" in args) is (sys.platform != "linux")
    assert "--disable-extensions" in args
    await synthetic_browser.owner.shutdown()


async def test_a_second_concurrent_session_starts_from_a_private_copy_of_the_sign_in(synthetic_browser, chrome154, monkeypatch):
    fake = synthetic_browser
    _saving_state(fake, monkeypatch)
    await fake.owner.run("a", ["open", "about:blank"])
    await fake.owner.run("b", ["open", "about:blank"])
    save = next(call for call in fake.calls if "state" in call)
    copy = Path(save[-1])
    assert _value(save, "--session") == fake.owner._sessions["a"].record.session
    assert copy.parent == browser_launch.profile_root() / "copies" and oct(copy.stat().st_mode & 0o777) == "0o600"
    second = _launches(fake)[1]
    restored = next(call for call in fake.calls if "state" in call and "load" in call)
    assert restored[-3:] == ("state", "load", str(copy))
    assert _value(restored, "--session") == fake.owner._sessions["b"].record.session
    assert fake.calls.index(restored) < fake.calls.index(second)
    assert "--profile" not in second and "--state" not in second
    await fake.owner.run("b", ["snapshot"])
    later = _launches(fake)[-1]
    assert sum("state" in call and "load" in call for call in fake.calls) == 1
    assert "--state" not in later  # Repeating restore resets the current page.
    assert _value(later, "--cdp") == _value(second, "--cdp")
    assert len(fake.chrome_calls) == 2
    assert _value(later, "--init-script") == _value(second, "--init-script")
    assert fake.owner._sessions["b"].record.copy_of == "a"
    await fake.owner.close_thread("b")
    assert not copy.exists()
    await fake.owner.shutdown()


async def test_a_failed_copy_restore_closes_the_copy_before_its_first_navigation(synthetic_browser, chrome154, monkeypatch):
    fake = synthetic_browser
    _saving_state(fake, monkeypatch)
    original = fake.owner._invoke
    handles = []

    async def invoke(argv, *, timeout):
        result = await original(argv, timeout=timeout)
        if argv[-3:-1] == ("state", "load"):
            handles.append(fake.owner._sessions["b"].chrome)
            return browser_cli.CommandResult(1, "", "fixture restore failure")
        return result

    monkeypatch.setattr(fake.owner, "_invoke", invoke)
    await fake.owner.run("a", ["open", "about:blank"])
    with pytest.raises(sessions.BrowserSessionError, match="startup failed.*saved sign-in could not be restored"):
        await fake.owner.run("b", ["open", "https://example.com"])
    assert "b" not in fake.owner._sessions and "a" in fake.owner._sessions
    assert not any("https://example.com" in call for call in fake.calls)
    assert len(handles) == 1 and handles[0].close.await_count == 1
    await fake.owner.shutdown()


async def test_a_missed_copy_starts_the_second_session_signed_out_without_failing_it(synthetic_browser, chrome154):
    fake = synthetic_browser
    await fake.owner.run("a", ["open", "about:blank"])
    rendered = await fake.owner.run("b", ["open", "about:blank"])
    assert "Saved sign-in wasn't used this time" in rendered
    assert not {"--state", "--profile"} & set(_launches(fake)[1])
    await fake.owner.shutdown()


async def test_unlaunched_holder_yields_the_profile_to_the_next_session(synthetic_browser, chrome154, monkeypatch):
    fake = synthetic_browser
    _saving_state(fake, monkeypatch)
    first = await fake.owner._admit("a", lease_id=None)
    assert first.record.binding is None
    await fake.owner.run("b", ["open", "about:blank"])
    assert f"--user-data-dir={browser_launch.profile_dir('b')}" in fake.chrome_calls[0]
    assert first.record.profile is None and first.record.copy_of == "b"
    await fake.owner.run("a", ["open", "about:blank"])
    assert any(call[-3:] == ("state", "load", str(browser_launch.copy_path(first.record.session)))
               for call in fake.calls)
    assert "--profile" not in _launches(fake)[1]
    await fake.owner.shutdown()


async def test_repeated_idle_transfers_redirect_waiting_copies(synthetic_browser, chrome154, monkeypatch):
    fake = synthetic_browser
    _saving_state(fake, monkeypatch)
    first = await fake.owner._admit("a", lease_id=None)
    second = await fake.owner._admit("b", lease_id=None)
    await fake.owner.run("c", ["open", "about:blank"])
    assert first.record.copy_of == second.record.copy_of == "c"
    await fake.owner.run("a", ["open", "about:blank"])
    assert any(call[-3:] == ("state", "load", str(browser_launch.copy_path(first.record.session)))
               for call in fake.calls)
    await fake.owner.shutdown()


async def test_the_profile_frees_up_for_the_next_session_once_its_holder_closes(synthetic_browser, chrome154):
    fake = synthetic_browser
    await fake.owner.run("a", ["open", "about:blank"])
    await fake.owner.close_thread("a")
    await fake.owner.run("b", ["open", "about:blank"])
    assert f"--user-data-dir={browser_launch.profile_dir('b')}" in fake.chrome_calls[1]
    await fake.owner.shutdown()


async def test_the_saved_profile_keeps_session_cookies_when_it_launches_again(synthetic_browser, chrome154):
    # Chrome deletes session cookies at startup unless it restores the last session (a session-cookie sign-in).
    await synthetic_browser.owner.run("a", ["open", "about:blank"])
    await synthetic_browser.owner.close_thread("a")
    await synthetic_browser.owner.run("a", ["open", "about:blank"])
    assert all("--restore-last-session" in argv for argv in synthetic_browser.chrome_calls)
    assert len(synthetic_browser.chrome_calls) == 2
    await synthetic_browser.owner.shutdown()


def _cdp_browsers(monkeypatch, cookies_by_endpoint):
    """Give each fixture Chrome its own endpoint and a CDP that reads and writes its cookies."""
    from host_access import browser_cdp, browser_chrome
    launched = browser_chrome.ChromeProcess.launch
    writes = []

    async def launch(executable, **kwargs):
        chrome = await launched(executable, **kwargs)
        chrome.endpoint = f"ws://127.0.0.1:39400/devtools/browser/{len(cookies_by_endpoint)}"
        cookies_by_endpoint[chrome.endpoint] = []
        return chrome

    class Cdp:
        def __init__(self, endpoint, **_):
            self.endpoint = endpoint

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def call(self, method, **params):
            if method == "Storage.getCookies":
                return {"cookies": cookies_by_endpoint[self.endpoint]}
            assert method == "Storage.setCookies"
            writes.append((self.endpoint, params["cookies"]))
            return {}

    monkeypatch.setattr(browser_chrome.ChromeProcess, "launch", launch)
    monkeypatch.setattr(browser_cdp, "CdpClient", Cdp)
    return writes


def _seeding(fake, monkeypatch, cookies):
    """A `state save` that writes the holder's cookies, as agent-browser's state file holds them."""
    import json
    original = fake.owner._invoke

    async def invoke(argv, *, timeout):
        if "state" in argv and "save" in argv:
            Path(argv[-1]).write_text(json.dumps({"cookies": cookies, "origins": []}))
        return await original(argv, timeout=timeout)

    monkeypatch.setattr(fake.owner, "_invoke", invoke)


_HOLDER_SIGNIN = {"name": "sid", "value": "holder", "domain": "example.com", "path": "/", "expires": -1, "session": True}
_COPY_SIGNIN = {"name": "auth", "value": "copy", "domain": "accounts.example.com", "path": "/", "expires": -1,
                "session": True, "size": 8, "httpOnly": True}


async def test_a_sign_in_made_on_a_copy_reaches_the_running_profile_without_moving_its_page(
        synthetic_browser, chrome154, monkeypatch):
    fake = synthetic_browser
    cookies = {}
    writes = _cdp_browsers(monkeypatch, cookies)
    _seeding(fake, monkeypatch, [_HOLDER_SIGNIN])
    await fake.owner.run("a", ["open", "about:blank"])
    await fake.owner.run("b", ["open", "about:blank"])
    holder, copy = (fake.owner._sessions[thread].chrome.endpoint for thread in ("a", "b"))
    # The copy kept the holder's sign-in it started from, and gained its own.
    cookies[copy] = [_HOLDER_SIGNIN, _COPY_SIGNIN]
    commands = len(fake.calls)
    await fake.owner.close_thread("b")
    assert writes == [(holder, [{key: value for key, value in _COPY_SIGNIN.items()
                                 if key not in {"expires", "session", "size"}}])]
    # Written through CDP only: no agent-browser `state load` (it navigates) reached the holder's page.
    assert not any("state" in call for call in fake.calls[commands:])
    assert not browser_session_launch.kept_signins_path(fake.owner._sessions["a"].record.profile).exists()
    await fake.owner.shutdown()


async def test_a_copys_sign_in_waits_for_the_profiles_next_launch_once_its_holder_has_closed(
        synthetic_browser, chrome154, monkeypatch):
    fake = synthetic_browser
    cookies = {}
    writes = _cdp_browsers(monkeypatch, cookies)
    _seeding(fake, monkeypatch, [_HOLDER_SIGNIN])
    await fake.owner.run("a", ["open", "about:blank"])
    await fake.owner.run("b", ["open", "about:blank"])
    profile = fake.owner._sessions["a"].record.profile
    cookies[fake.owner._sessions["b"].chrome.endpoint] = [_HOLDER_SIGNIN, _COPY_SIGNIN]
    await fake.owner.close_thread("a")
    await fake.owner.close_thread("b")
    kept = browser_session_launch.kept_signins_path(profile)
    assert writes == [] and kept.is_file() and oct(kept.stat().st_mode & 0o777) == "0o600"
    await fake.owner.run("c", ["open", "about:blank"])
    assert fake.owner._sessions["c"].record.profile == profile
    assert [(endpoint, [cookie["name"] for cookie in written]) for endpoint, written in writes] == [
        (fake.owner._sessions["c"].chrome.endpoint, ["auth"])]
    assert not kept.exists()
    await fake.owner.shutdown()


async def test_a_launch_still_starts_when_its_applied_sign_ins_cannot_be_cleared(
        synthetic_browser, chrome154, monkeypatch):
    fake = synthetic_browser
    cookies = {}
    writes = _cdp_browsers(monkeypatch, cookies)
    _seeding(fake, monkeypatch, [_HOLDER_SIGNIN])
    await fake.owner.run("a", ["open", "about:blank"])
    await fake.owner.run("b", ["open", "about:blank"])
    profile = fake.owner._sessions["a"].record.profile
    cookies[fake.owner._sessions["b"].chrome.endpoint] = [_HOLDER_SIGNIN, _COPY_SIGNIN]
    await fake.owner.close_thread("a")
    await fake.owner.close_thread("b")

    def unwritable(path, cookies):
        raise OSError("read-only")

    monkeypatch.setattr(browser_session_launch, "_write_cookies", unwritable)
    await fake.owner.run("c", ["open", "about:blank"])
    assert fake.owner._sessions["c"].record.terminal_state == "running"
    assert [cookie["name"] for _endpoint, written in writes for cookie in written] == ["auth"]
    assert browser_session_launch.kept_signins_path(profile).is_file()
    await fake.owner.shutdown()


async def test_production_browser_route_uses_profile_only_for_resolved_owners(synthetic_browser, chrome154, monkeypatch, tmp_path):
    from host_access.processes import ProcessOwner
    from infra import database
    from tools import browser_runtime

    db_path = tmp_path / "owners.db"
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE threads (id TEXT, user_id TEXT)")
    db.executemany("INSERT INTO threads VALUES (?, ?)", [("owned", "user-1"), ("bad", "../escape"), ("null", None)])
    db.commit()
    db.close()
    monkeypatch.setattr(browser_launch, "_owner_key", _REAL_OWNER_KEY)
    monkeypatch.setattr(database, "get_db", lambda: sqlite3.connect(db_path))
    monkeypatch.setattr(browser_runtime, "_check_installed", lambda: None)
    process_owner = ProcessOwner.__new__(ProcessOwner)
    process_owner._browser_owner = synthetic_browser.owner
    monkeypatch.setattr(browser_runtime, "PROCESS_OWNER", process_owner)
    for thread in ("owned", "missing", "bad", "null"):
        result = await browser_runtime._run(["open", "about:blank"], thread_id=thread)
        assert result == "ok" if thread == "owned" else "Saved sign-in wasn't used this time" in result
    calls = _launches(synthetic_browser)
    assert f"--user-data-dir={browser_launch.profile_root() / 'user-1'}" in synthetic_browser.chrome_calls[0]
    assert all(not {"--profile", "--state"} & set(call) for call in calls[1:])
    def unavailable():
        raise sqlite3.OperationalError("fixture database unavailable")
    monkeypatch.setattr(database, "get_db", unavailable)
    assert "Saved sign-in wasn't used this time" in await browser_runtime._run(["open", "about:blank"], thread_id="db-error")
    assert not {"--profile", "--state"} & set(_launches(synthetic_browser)[-1])
    await synthetic_browser.owner.shutdown()


@pytest.mark.parametrize("args", [
    ["cookies"], ["cookies", "get"], ["state", "save", "/tmp/x.json"], ["state", "load", "/tmp/x.json"],
    ["storage", "local"], ["auth", "list"], ["profiles"], ["network", "har", "start"], ["network", "requests"],
    ["open", "https://example.com", "--state", "/tmp/x.json"], ["open", "about:blank", "--profile", "/tmp/p"],
    ["open", "about:blank", "--profile=/tmp/p"], ["open", "about:blank", "--state=/tmp/x.json"],
    ["open", "about:blank", "--restore"], ["stream", "enable"], ["stream", "status"],
    ["dashboard"], ["connect", "39400"], ["get", "cdp-url"],
])
async def test_no_agent_route_can_read_or_redirect_the_saved_sign_in(synthetic_browser, args):
    """C2: the shared command path refuses every command that dumps cookies or saved state."""
    fake = synthetic_browser
    with pytest.raises(sessions.BrowserSessionError, match="not available to agents"):
        await fake.owner.run("a", args)
    with pytest.raises(sessions.BrowserSessionError, match="not available to agents"):
        await fake.owner.run("a", args, pane=True)
    assert fake.calls == []


def test_the_profile_folder_is_hard_floor_closed_to_every_agent_shell_and_lease(tmp_path):
    """C1: a protected shell, and a lease, are refused the cookie database."""
    state_root = tmp_path / "state"
    policy = ProtectedHostPathPolicy(home=tmp_path / "home", state_root=state_root)
    profile = state_root / browser_launch.PROFILE_ROOT_NAME
    for path in (profile, profile / "instance" / "Default" / "Cookies", profile / "copies" / "s.json"):
        for for_write in (False, True):
            for check in (policy.denial, policy.ordinary_denial):
                message = check(path, for_write=for_write)
                assert message and "No host-access lease can open it." in message, (path, for_write)
        assert policy.hard_floor_denial(path)
    assert f'(deny file-read* file-write* (subpath "{profile}"))' in policy.sandbox_rules()
    profile.mkdir(parents=True)
    link = tmp_path / "profile-link"
    link.symlink_to(profile, target_is_directory=True)
    for for_write in (False, True):
        assert policy.denial(link / "synthetic-cookie", for_write=for_write)
        assert policy.ordinary_denial(link / "synthetic-cookie", for_write=for_write)
    assert policy.hard_floor_denial(link / "synthetic-cookie")


def test_the_stealth_script_ships_next_to_the_launcher():
    assert browser_launch.STEALTH_INIT_SCRIPT.is_file()
    assert browser_launch.user_agent(None) is None
    assert "Chrome/154.0.0.0" in browser_launch.user_agent("154")
