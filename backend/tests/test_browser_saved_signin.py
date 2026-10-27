"""The shared saved sign-in profile, its concurrent copies, the agent-route refusals and headless stealth."""
from pathlib import Path
import sqlite3

import pytest
from infra.config import STATE_ROOT
from host_access import browser_cli, browser_launch, browser_sessions as sessions
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


async def test_a_shared_browser_uses_headless_stealth_user_agent(synthetic_browser, chrome154):
    fake = synthetic_browser
    await fake.owner.run("a", ["open", "about:blank"], show_window=True)
    args = fake.chrome_calls[0]
    ua = next(arg for arg in args if arg.startswith("--user-agent="))
    assert "HeadlessChrome" not in ua
    assert "--disable-blink-features=AutomationControlled" in args
    await fake.owner.shutdown()


async def test_the_environments_own_launch_args_survive(synthetic_browser, chrome154, monkeypatch):
    monkeypatch.setenv("AGENT_BROWSER_ARGS", "--no-sandbox")
    await synthetic_browser.owner.run("a", ["open", "about:blank"])
    assert "--no-sandbox" in synthetic_browser.chrome_calls[0]
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
