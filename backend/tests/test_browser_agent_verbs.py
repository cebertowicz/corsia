"""Keys, dropdown choice, scrolling, hover and history on a saved sign-in: each verb is one fixed shape."""
import asyncio

import pytest

from host_access import browser_agent_verbs as verbs
from host_access import browser_cdp, browser_control, browser_launch, browser_step_lifecycle
from host_access import browser_session_commands as commands
from host_access.browser_session_state import BrowserSessionError
from tests.browser_process_fixture import synthetic_browser  # noqa: F401

ADMITTED = [
    *(["press", key] for key in verbs.KEYS),
    ["select", "@e5", "large"], ["select", "#size", "Large (L)"], ["select", "select[name=qty]", "2"],
    ["scroll", "down"], ["scroll", "up", "500"], ["scroll", "left", "40"], ["scroll", "right", "5000"],
    ["scrollintoview", "@e15"], ["scrollintoview", "#footer"],
    ["hover", "@e4"], ["hover", ".menu > li:first-child"],
    ["back"], ["forward"],
]
REFUSED = [
    # keys: anything off the list, modifiers, function keys, printable text, wrong case or spelling
    ["press", "Control+a"], ["press", "Meta+r"], ["press", "Control+Shift+Tab"], ["press", "Alt+ArrowLeft"],
    ["press", "F5"], ["press", "F12"], ["press", "a"], ["press", "hello"], ["press", " "], ["press", "enter"],
    ["press", "Page Up"], ["press", "Shift+a"], ["press", "Enter", "Enter"], ["press"], ["press", "-h"],
    # arguments that start with a dash
    ["select", "-x", "v"], ["select", "@e5", "--help"], ["hover", "--help"], ["scrollintoview", "-s"],
    ["scroll", "--selector", "#a"], ["scroll", "down", "-5"], ["scroll", "down", "-s", "#pane"],
    ["back", "--help"], ["forward", "-v"],
    # selector forms that run script, or a locator engine other than a ref or CSS
    ["hover", "javascript:alert(1)"], ["select", "text=Sign in", "v"], ["hover", "xpath=//a"],
    ["hover", "//a[@id='x']"], ["scrollintoview", "(//a)[1]"], ["hover", "js=document.title"],
    ["hover", "css=#a >> text=b"], ["scrollintoview", "#a >> nth=0"], ["hover", "a\nb"],
    ["select", "eval(1)", "v"],
    # Playwright's internal engines carry a colon in the prefix
    ["hover", "internal:text=Pay"], ["scrollintoview", "internal:control=enter-frame"],
    ["select", "internal:role=button", "v"], ["hover", "internal:attr=[id=a]"],
    # shape: several values, extra arguments, bad directions and amounts
    ["select", "@e5", "a", "b"], ["select", "@e5"], ["hover", "@e1", "@e2"], ["scrollintoview"],
    ["scroll"], ["scroll", "sideways"], ["scroll", "down", "lots"], ["scroll", "down", "0"],
    ["scroll", "down", "5001"], ["scroll", "down", "10", "extra"], ["scroll", "down", "1e3"],
]


@pytest.mark.parametrize("args", ADMITTED)
def test_each_admitted_shape_is_admitted_on_a_saved_session(args):
    assert commands._saved_command(args, pane=False)
    assert verbs.admitted(args)


@pytest.mark.parametrize("args", REFUSED)
def test_each_excluded_shape_stays_refused_on_a_saved_session(args):
    assert not commands._saved_command(args, pane=False)


def test_pane_commands_are_unchanged():
    for args in (["press", "Enter"], ["select", "@e1", "v"], ["scroll", "down"], ["hover", "@e1"]):
        assert commands._saved_command(args, pane=False)
        assert not commands._saved_command(args, pane=True)
    assert commands._saved_command(["back"], pane=True) and commands._saved_command(["forward"], pane=True)
    with pytest.raises(BrowserSessionError, match="only navigates"):
        commands._validate("a", ["press", "Enter"], 5, None, True)


@pytest.fixture
def saved(synthetic_browser, monkeypatch):
    monkeypatch.setattr(browser_launch, "_owner_key", lambda _: "owner")
    browser_control._guarded_visits.clear()
    browser_step_lifecycle._steps.clear()
    browser_step_lifecycle._answers.clear()
    yield synthetic_browser
    browser_control._guarded_visits.clear()
    browser_step_lifecycle._steps.clear()
    browser_step_lifecycle._answers.clear()


@pytest.mark.parametrize("args", [["press", "Enter"], ["select", "@e5", "large"], ["scroll", "down", "300"],
                                  ["scrollintoview", "@e9"], ["hover", "@e4"], ["back"], ["forward"]])
async def test_saved_session_runs_each_verb_and_returns_no_page_content(saved, args):
    saved.result = type(saved.result)(0, '{"success":true,"data":{"title":"Account 4417","url":"https://bank.test/a"}}', '')
    result = await saved.owner.run("a", args)
    assert result == verbs.ACKNOWLEDGED
    assert [call[-len(args):] for call in saved.calls if call[-len(args):] == tuple(args)]
    await saved.owner.shutdown()


@pytest.mark.parametrize("args", [["press", "F5"], ["press", "Control+a"], ["press", "x"]])
async def test_a_key_off_the_list_is_refused_with_the_allowed_keys(saved, args):
    with pytest.raises(BrowserSessionError) as refused:
        await saved.owner.run("a", args)
    assert "Shift+Tab" in str(refused.value) and "PageDown" in str(refused.value) and "browser_fill" in str(refused.value)
    assert saved.calls == []
    await saved.owner.shutdown()


@pytest.mark.parametrize("args", [["select", "@e5", "a", "b"], ["hover", "text=Pay"], ["scroll", "down", "-s", "#x"]])
async def test_other_excluded_shapes_are_refused_before_any_command_runs(saved, args):
    with pytest.raises(BrowserSessionError, match="unavailable in a browser session with a saved sign-in"):
        await saved.owner.run("a", args)
    assert saved.calls == []
    await saved.owner.shutdown()


@pytest.mark.parametrize("args", [["press", "Enter"], ["select", "@e5", "large"], ["scroll", "down"],
                                  ["scrollintoview", "@e9"], ["hover", "@e4"], ["back"], ["forward"]])
async def test_the_page_access_guard_runs_before_each_verb(saved, monkeypatch, args):
    seen = []

    async def refuse(thread_id, _probe, command, **_kwargs):
        seen.append((thread_id, command))
        raise BrowserSessionError("This page asks for a password")

    monkeypatch.setattr(browser_control, "require_page_access", refuse)
    with pytest.raises(BrowserSessionError, match="asks for a password"):
        await saved.owner.run("a", args)
    assert seen == [("a", tuple(args))]
    assert not [call for call in saved.calls if call[-len(args):] == tuple(args)]
    await saved.owner.shutdown()


@pytest.mark.parametrize("held", ["guard", "open_step", "settled_step", "visible_password"])
@pytest.mark.parametrize("direction", ["back", "forward"])
async def test_history_after_a_held_page_loads_the_page_fresh(saved, monkeypatch, held, direction):
    reopened = []

    async def reopen_fresh(endpoint):
        reopened.append(endpoint)

    monkeypatch.setattr(commands, "reopen_fresh", reopen_fresh)
    visible = frozenset({7}) if held == "visible_password" else frozenset()

    async def probe(endpoint, _known):
        return browser_cdp.PasswordProbe("visit", "bank.test", visible, endpoint, present_nodes=visible)

    monkeypatch.setattr(browser_cdp, "page_password_probe", probe)
    # The open step would refuse the command first, so only the guard decision is set up for it.
    if held == "guard":
        browser_control._guarded_visits["a"] = {"earlier": browser_control.PasswordReadGuard("earlier", frozenset({7}))}
    elif held == "settled_step":
        browser_step_lifecycle._answers["a"] = {("person", "earlier"): browser_step_lifecycle.Answer(1)}
    elif held == "open_step":
        browser_step_lifecycle._steps["a"] = object()
        monkeypatch.setattr(browser_control, "require_page_access", _allow)
    if held == "visible_password":
        monkeypatch.setattr(browser_control, "require_page_access", _allow)
    assert await saved.owner.run("a", [direction]) == verbs.ACKNOWLEDGED
    assert reopened == ["ws://synthetic.test/devtools/page/1"]
    browser_step_lifecycle._steps.pop("a", None)
    await saved.owner.shutdown()


async def _allow(*_args, **_kwargs):
    return None


@pytest.mark.parametrize("direction", ["back", "forward"])
async def test_history_in_a_session_that_held_no_page_does_not_reload(saved, monkeypatch, direction):
    async def reopen_fresh(_endpoint):
        raise AssertionError("a page nobody held is not reloaded")

    monkeypatch.setattr(commands, "reopen_fresh", reopen_fresh)
    assert await saved.owner.run("a", [direction]) == verbs.ACKNOWLEDGED
    await saved.owner.shutdown()


async def test_a_failed_check_after_history_fails_closed(saved, monkeypatch):
    calls = {"count": 0}

    async def probe(endpoint, _known):
        calls["count"] += 1
        if calls["count"] > 1:  # the guard's own probe passes; the one after the history move fails
            raise RuntimeError("target gone")
        return browser_cdp.PasswordProbe("visit", "bank.test", frozenset(), endpoint, present_nodes=frozenset())

    monkeypatch.setattr(browser_cdp, "page_password_probe", probe)
    with pytest.raises(BrowserSessionError, match="couldn't be checked"):
        await saved.owner.run("a", ["back"])
    await saved.owner.shutdown()


async def _held_page(saved, monkeypatch, reopened):
    """A session whose page the person held, with a reload that records the page it reloads."""
    async def reopen_fresh(endpoint):
        reopened.append(endpoint)

    monkeypatch.setattr(commands, "reopen_fresh", reopen_fresh)
    browser_step_lifecycle._answers["a"] = {("person", "earlier"): browser_step_lifecycle.Answer(1)}
    monkeypatch.setattr(browser_control, "require_page_access", _allow)


@pytest.mark.parametrize("outcome", ["error", "timeout", "raises"])
@pytest.mark.parametrize("direction", ["back", "forward"])
async def test_history_that_errors_or_times_out_still_loads_a_held_page_fresh(saved, monkeypatch, outcome, direction):
    reopened = []
    await _held_page(saved, monkeypatch, reopened)
    if outcome == "error":
        saved.result = type(saved.result)(1, "", "navigation failed")
    elif outcome == "timeout":
        saved.result = type(saved.result)(-1, "", "", timed_out=True, completion="interrupted")
    else:
        async def fail(*_args, **_kwargs):
            raise BrowserSessionError("cancelled")

        monkeypatch.setattr(saved.owner, "_run_admitted", fail)
    try:
        await saved.owner.run("a", [direction])
    except BrowserSessionError:
        pass
    assert reopened == ["ws://synthetic.test/devtools/page/1"]
    await saved.owner.shutdown()


def _fail_after(name, module, calls, passes):
    """Replace module.name so its first ``passes`` uses pass and every later one raises."""
    original = getattr(module, name)

    async def wrapper(*args, **kwargs):
        calls.append(name)
        if len(calls) > passes:
            raise RuntimeError("target gone")
        return await original(*args, **kwargs)

    return wrapper


@pytest.mark.parametrize("failing", ["page_endpoint", "page_password_probe", "reopen_fresh"])
@pytest.mark.parametrize("blank", ["works", "fails", "cancelled"])
async def test_a_history_failure_leaves_the_page_unreadable(saved, monkeypatch, failing, blank):
    """If the endpoint, the check or the reload fails, the restored page is blanked, or the session closed."""
    reopened = []
    await _held_page(saved, monkeypatch, reopened)
    calls = []
    if failing == "reopen_fresh":
        async def reopen_fresh(_endpoint):
            raise RuntimeError("target gone")

        monkeypatch.setattr(commands, "reopen_fresh", reopen_fresh)
    else:
        # The guard resolves the page once before the verb (the probe is skipped by _allow); the history check is next.
        monkeypatch.setattr(browser_cdp, failing, _fail_after(failing, browser_cdp, calls, 1 if failing == "page_endpoint" else 0))
    if blank != "works":
        real = saved.owner._run_admitted

        async def run_admitted(runtime, args, **kwargs):
            if tuple(args) == ("open", "about:blank"):
                raise asyncio.CancelledError() if blank == "cancelled" else BrowserSessionError("browser is gone")
            return await real(runtime, args, **kwargs)

        monkeypatch.setattr(saved.owner, "_run_admitted", run_admitted)
    expected = asyncio.CancelledError if blank == "cancelled" else (BrowserSessionError, RuntimeError)
    with pytest.raises(expected):
        await saved.owner.run("a", ["back"])
    blanked = [call for call in saved.calls if call[-2:] == ("open", "about:blank")]
    if blank == "works":
        assert len(blanked) == 1
    else:
        # Nothing could blank the page, so the session is gone and the next read has nothing to read.
        assert "a" not in saved.owner._sessions
        with pytest.raises(BrowserSessionError, match="no longer open"):
            await saved.owner.run("a", ["snapshot"], session="existing")
    await saved.owner.shutdown()


COVER_TEXT = ("Element '#pay' is covered by <div class=\"modal\" data-account=\"4417\">Account 4417</div> at its click "
              "point, so the input would land on that element instead.")


@pytest.mark.parametrize("args", [["hover", "#pay"], ["select", "#pay", "Large"], ["scrollintoview", "#pay"]])
@pytest.mark.parametrize(("stderr", "expected"), [
    (COVER_TEXT, verbs.COVERED),
    ("no option labelled 'Large (L)' among: Small (S), Medium (M)", verbs.UNUSABLE),
    ("Element not found. Verify the selector is correct and the element exists in the DOM.", verbs.NOT_FOUND),
])
async def test_a_target_verbs_error_carries_nothing_from_the_page(saved, args, stderr, expected):
    saved.result = type(saved.result)(1, "", stderr)
    result = await saved.owner.run("t1", args)
    assert result == expected
    assert "4417" not in result and "<div" not in result and "Small" not in result
    await saved.owner.shutdown()


async def test_the_error_of_another_command_is_left_as_it_was(saved):
    saved.result = type(saved.result)(1, "", COVER_TEXT)
    assert "Account 4417" in await saved.owner.run("t1", ["click", "#pay"])
    await saved.owner.shutdown()


async def test_a_session_without_a_saved_sign_in_is_unchanged(synthetic_browser, monkeypatch):
    monkeypatch.setattr(browser_launch, "_owner_key", lambda _: None)
    await synthetic_browser.owner.run("a", ["open", "about:blank"])
    assert await synthetic_browser.owner.run("a", ["eval", "1+1"]) == "ok"
    assert await synthetic_browser.owner.run("a", ["press", "Control+a"]) == "ok"
    assert await synthetic_browser.owner.run("a", ["mouse", "wheel", "10"]) == "ok"
    await synthetic_browser.owner.shutdown()


def test_held_pages_reads_only_step_state():
    browser_step_lifecycle._steps.clear()
    browser_step_lifecycle._answers.clear()
    assert not browser_step_lifecycle.held_pages("a")
    browser_step_lifecycle._answers["a"] = {("password", "v"): browser_step_lifecycle.Answer(1)}
    assert browser_step_lifecycle.held_pages("a")
    assert not browser_step_lifecycle.held_pages("b")
    browser_step_lifecycle._answers.clear()


@pytest.mark.parametrize(("arguments", "expected"), [
    ({"action": "press", "key": "Enter"}, ["press", "Enter"]),
    ({"action": "press", "key": "Shift+Tab"}, ["press", "Shift+Tab"]),
    ({"action": "select", "selector": "@e5", "value": "large"}, ["select", "@e5", "large"]),
    ({"action": "scroll"}, ["scroll", "down"]),
    ({"action": "scroll", "direction": "up", "amount": 500}, ["scroll", "up", "500"]),
    ({"action": "scroll", "selector": "#footer"}, ["scrollintoview", "#footer"]),
    ({"action": "hover", "selector": "@e4"}, ["hover", "@e4"]),
    ({"action": "back"}, ["back"]),
    ({"action": "forward"}, ["forward"]),
])
async def test_the_tool_runs_one_fixed_shape_for_the_exact_thread(monkeypatch, arguments, expected):
    from tools import browser_input

    calls = []

    async def run(args, *, thread_id, **_options):
        calls.append((args, thread_id))
        return "ok"

    monkeypatch.setattr(browser_input, "_run", run)
    assert await browser_input.BrowserInputTool().execute(**arguments, _thread_id="authoritative-thread") == "ok"
    assert calls == [(expected, "authoritative-thread")]
    assert commands._saved_command(expected, pane=False)


@pytest.mark.parametrize("arguments", [
    {"action": "press", "key": "Control+a"}, {"action": "press", "key": "F5"}, {"action": "press"},
    {"action": "select", "selector": "@e1"}, {"action": "select", "value": "v"}, {"action": "hover"},
    {"action": "scroll", "direction": "sideways"}, {"action": "reload"},
])
async def test_the_tool_refuses_off_list_input_without_running_a_command(monkeypatch, arguments):
    from tools import browser_input

    async def run(*_args, **_options):
        raise AssertionError("no command should run")

    monkeypatch.setattr(browser_input, "_run", run)
    refused = await browser_input.BrowserInputTool().execute(**arguments, _thread_id="a")
    assert refused.startswith("Error:")
    if arguments.get("key") == "Control+a":
        assert "Shift+Tab" in refused and "browser_fill" in refused


def test_the_schema_holds_the_key_list_and_the_actions():
    from tools.browser_input import ACTIONS, BrowserInputTool

    properties = BrowserInputTool.parameters["properties"]
    assert properties["key"]["enum"] == list(verbs.KEYS)
    assert properties["action"]["enum"] == list(ACTIONS)
    assert BrowserInputTool.parameters["required"] == ["action"]


async def test_the_tool_requires_exact_backend_identity():
    from tools.browser_input import BrowserInputTool

    with pytest.raises(PermissionError, match="thread identity"):
        await BrowserInputTool().execute("back")


def test_the_tool_is_cataloged_in_the_browser_pack():
    from tools.catalog_entries import TOOL_CATALOG

    assert TOOL_CATALOG["browser_input"].category == "browser"
