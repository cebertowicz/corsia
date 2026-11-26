"""After a saved login is filled: the filled document runs under the filled-field allowlist until a new document
commits in its tab, and the whole session runs only the saved-sign-in command shapes until it ends.

`mark_saved_fill` is what the fill executor calls before it types. The page-guard tests drive `check_page_access`
on a page with no password field showing, so the only reason for a refusal is the mark; the session tests drive
the agent command path (`browser_session_commands.run`) with the real page guard and a fake page probe.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from host_access import (
    browser_cdp, browser_cli, browser_control, browser_output_blanking, browser_pane_state, browser_saved_fills,
    browser_session_commands, browser_step_lifecycle as lifecycle,
)
from host_access.browser_cdp import PasswordProbe
from host_access.browser_session_state import BrowserRuntime, BrowserSessionError, BrowserSessionRecord

THREAD = "thread-saved-fill"
FILLED, OTHER = "loader-filled", "loader-other"


@pytest.fixture(autouse=True)
def _fresh():
    held = (lifecycle._steps, lifecycle._answers, lifecycle._sessions, lifecycle._probe_locks,
            browser_control._guarded_visits, browser_saved_fills._saved_fills)
    for each in held:
        each.clear()
    yield
    for each in held:
        each.clear()


@pytest.fixture(autouse=True)
def _no_saved_values(monkeypatch):
    """No saved value by default, so a screenshot's check reads no store; a test that needs one sets it."""
    monkeypatch.setattr(browser_output_blanking, "_saved_values", frozenset)


def _page(visit: str):
    async def probe(*_args):
        return PasswordProbe(visit, "example.com", frozenset(), "target-1", present_nodes=frozenset())

    return probe


async def _access(args: tuple[str, ...], session: str | None = "session-1",
                  visit: str = FILLED) -> browser_control.PageAccess:
    async def resolve(_target):
        return 7

    return await browser_control.check_page_access(THREAD, _page(visit), args, resolve=resolve, session=session)


async def _run(monkeypatch, args: tuple[str, ...], *, visit: str = FILLED, session: str = "session-1",
               output: str = "ok", page: browser_cli.CommandResult | None = None,
               ran: list[tuple[str, ...]] | None = None) -> str:
    """One agent command through the real command path and page guard, on a page whose document is `visit`.

    `page` is what a snapshot the server takes for itself returns; `ran` collects every command run.
    """
    runtime = BrowserRuntime(BrowserSessionRecord(THREAD, session, binding=object()))
    ran = [] if ran is None else ran

    class Owner:
        _queued_actions = 0
        _native = SimpleNamespace(version="0.31.3")

        async def _pin_native(self):
            pass

        async def _admit(self, *_args, **_kwargs):
            return runtime

        async def _run_admitted(self, _runtime, command_args, **_kwargs):
            ran.append(tuple(command_args))
            if page is not None and tuple(command_args) == ("snapshot",):
                return page, False
            return browser_cli.CommandResult(0, output, ""), False

        def _model_result(self, result, _record):
            return result.stdout

        def session_identity(self, _thread_id):
            return runtime.record.session

    async def endpoint(_command):
        return "ws://page"

    async def finished(*_args):
        pass

    monkeypatch.setattr(browser_cli, "check_local_mode", lambda _args: {})
    monkeypatch.setattr(browser_cdp, "page_endpoint", endpoint)
    monkeypatch.setattr(browser_cdp, "page_password_probe", lambda _endpoint, known: _page(visit)(known))
    monkeypatch.setattr(browser_pane_state, "has_pane", lambda _thread: False)
    monkeypatch.setattr(browser_pane_state, "agent_acted", lambda _thread: None)
    monkeypatch.setattr(browser_pane_state, "page_command_starting", lambda *_args: None)
    monkeypatch.setattr(browser_pane_state, "page_command_finished", finished)
    return await browser_session_commands.run(Owner(), THREAD, args, lease_id=None, timeout=5,
                                              show_window=None, session=session, pane=False)


# --- The filled document ------------------------------------------------------------------------------------

REFUSED_ON_THE_FILLED_PAGE = [
    ("eval", "document.title"),
    ("wait", "--fn", "window.x"),
    ("get", "value", "@e1"),
    ("get", "html", "@e1"),
    ("get", "attr", "@e1", "value"),
    ("network", "route", "https://example.com/*"),
    ("screenshot", "shot.png"),
    ("snapshot", "-c"),
    ("click", "input[value^=a]"),
]
ALLOWED_ON_THE_FILLED_PAGE = [
    ("snapshot",), ("snapshot", "-i"), ("click", "@e2"), ("fill", "@e3", "text"), ("get", "text", "@e1"),
    ("get", "url"), ("get", "title"), ("open", "https://example.com/next"), ("reload",), ("wait", "500"),
    ("press", "Enter"), ("tab",),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("args", REFUSED_ON_THE_FILLED_PAGE)
async def test_on_the_filled_page_scripts_value_reads_routes_and_screenshots_are_refused(args):
    browser_saved_fills.mark_saved_fill(THREAD, "session-1", FILLED)
    with pytest.raises(BrowserSessionError) as refused:
        await _access(args)
    assert str(refused.value) == browser_control.SAVED_FILL_REFUSED


@pytest.mark.asyncio
@pytest.mark.parametrize("args", ALLOWED_ON_THE_FILLED_PAGE)
async def test_on_the_filled_page_commands_that_cant_show_it_pass_with_snapshot_values_hidden(args):
    browser_saved_fills.mark_saved_fill(THREAD, "session-1", FILLED)
    access = await _access(args)
    # filled_field_visit is what makes the command path hide a snapshot's field values.
    assert access.filled_field_visit is True and access.saved_fill is True


@pytest.mark.asyncio
async def test_a_new_document_in_the_tab_hands_over_from_the_page_rule_to_the_session_rule(monkeypatch):
    browser_saved_fills.mark_saved_fill(THREAD, "session-1", FILLED)
    # The page guard lets the new document go...
    access = await _access(("eval", "1"), visit=OTHER)
    assert access.filled_field_visit is False and access.saved_fill is False
    # ...a screenshot runs there, though not on the filled page...
    assert await _run(monkeypatch, ("screenshot", "shot.png"), visit=OTHER) == "ok"
    with pytest.raises(BrowserSessionError) as refused:
        await _run(monkeypatch, ("screenshot", "shot.png"), visit=FILLED)
    assert str(refused.value) == browser_control.SAVED_FILL_REFUSED
    # ...and the session's own rule still refuses scripts and every other unlisted command on it.
    for args in (("eval", "1"), ("get", "text", "@e1"), ("dblclick", "@e1"), ("network", "route", "**/*")):
        with pytest.raises(BrowserSessionError) as refused:
            await _run(monkeypatch, args, visit=OTHER)
        assert str(refused.value) == browser_saved_fills.SAVED_FILL_SESSION_REFUSED, args


@pytest.mark.asyncio
@pytest.mark.parametrize(("marked", "seen"), [("", OTHER), (FILLED, "")])
async def test_a_document_id_that_wasnt_read_counts_as_the_filled_page(marked, seen):
    browser_saved_fills.mark_saved_fill(THREAD, "session-1", marked)
    with pytest.raises(BrowserSessionError) as refused:
        await _access(("eval", "1"), visit=seen)
    assert str(refused.value) == browser_control.SAVED_FILL_REFUSED


@pytest.mark.asyncio
async def test_a_field_the_person_filled_keeps_its_own_refusal(monkeypatch):
    # The person's password step ended on this visit and no saved login was filled.
    monkeypatch.setattr(lifecycle, "finished_visit", lambda _thread, visit: visit == FILLED)
    with pytest.raises(BrowserSessionError) as refused:
        await _access(("eval", "1"))
    assert str(refused.value) == browser_control.FILLED_FIELD_REFUSED


@pytest.mark.asyncio
@pytest.mark.parametrize(("saved_fill", "line"), [
    (True, browser_control.SAVED_FILL_SNAPSHOT_WITHHELD),
    (False, browser_control.FILLED_FIELD_SNAPSHOT_WITHHELD),
])
async def test_a_snapshot_whose_values_cant_be_hidden_is_withheld_with_its_own_line(monkeypatch, saved_fill, line):
    if saved_fill:
        browser_saved_fills.mark_saved_fill(THREAD, "session-1", FILLED)
    else:
        monkeypatch.setattr(lifecycle, "finished_visit", lambda _thread, visit: visit == FILLED)
    assert await _run(monkeypatch, ("snapshot",), output="not a snapshot") == line


def test_the_saved_fill_lines_say_what_holds_and_until_when():
    page, withheld, session = (browser_control.SAVED_FILL_REFUSED, browser_control.SAVED_FILL_SNAPSHOT_WITHHELD,
                               browser_saved_fills.SAVED_FILL_SESSION_REFUSED)
    for line in (page, withheld, session):
        # The server filled the field, so no line says the person did; each holds for a login or an item.
        assert "A saved login or saved item was filled" in line and "person filled" not in line
    assert "on this page, so until you open another page or reload this one" in page
    # Only a screenshot or a CSS-selector target passes the session limit and then meets the page's own refusal.
    assert "screenshots are refused here and each element must be named by its @ref" in page
    assert "still runs only the commands allowed after a saved fill, on every page" in page
    assert "still work" not in page and "network routes" not in page
    assert "on this page" in withheld
    assert "in this browser session, so until the session ends only these commands run, on every page" in session
    assert "Scripts, reading field values, network routes and every other command are refused." in session
    # The agent can't end the session itself, so the line names the next step.
    assert session.endswith("tell the person in chat what you need it for.")


# --- The session ----------------------------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("args", [("snapshot",), ("get", "url"), ("click", "@e2"), ("wait", "500"), ("tab",),
                                  ("open", "https://example.com/next"), ("reload",)])
async def test_in_the_session_the_saved_sign_in_shapes_still_run_on_any_page(monkeypatch, args):
    browser_saved_fills.mark_saved_fill(THREAD, "session-1", FILLED)
    assert await _run(monkeypatch, args, visit=OTHER) == "ok"


@pytest.mark.asyncio
async def test_without_a_fill_the_session_rule_does_not_apply(monkeypatch):
    assert await _run(monkeypatch, ("eval", "1"), visit=OTHER) == "ok"


@pytest.mark.asyncio
async def test_closing_the_browser_ends_the_mark_for_a_later_session_with_the_same_id(monkeypatch):
    # The pane and the e2e tests reuse the session id "existing", so the mark must end with the session itself.
    browser_saved_fills.mark_saved_fill(THREAD, "existing", FILLED)
    with pytest.raises(BrowserSessionError):
        await _run(monkeypatch, ("eval", "1"), visit=OTHER, session="existing")
    browser_control.browser_closed(THREAD)
    assert await _run(monkeypatch, ("eval", "1"), visit=FILLED, session="existing") == "ok"
    access = await _access(("eval", "1"), session="existing")
    assert access.filled_field_visit is False and access.saved_fill is False


@pytest.mark.asyncio
@pytest.mark.parametrize(("marked", "command_session"), [("session-1", None), (None, "session-2"), (None, None)])
async def test_a_mark_or_command_without_a_session_id_fails_closed(marked, command_session):
    browser_saved_fills.mark_saved_fill(THREAD, marked, FILLED)
    assert browser_saved_fills.saved_fill_session(THREAD, command_session) is True
    with pytest.raises(BrowserSessionError) as refused:
        await _access(("eval", "1"), session=command_session)
    assert str(refused.value) == browser_control.SAVED_FILL_REFUSED


@pytest.mark.asyncio
async def test_a_mark_for_one_session_does_not_reach_another(monkeypatch):
    browser_saved_fills.mark_saved_fill(THREAD, "session-1", FILLED)
    assert (await _access(("eval", "1"), session="session-2")).filled_field_visit is False
    assert await _run(monkeypatch, ("eval", "1"), session="session-2") == "ok"


# --- A later page that shows a saved value --------------------------------------------------------------------

VALUE = "sk-test-4417-1234-5678"  # gitleaks:allow - a fake fixture value
SHOT = ("screenshot", "shot.png")


def _snapshot(text: str, returncode: int = 0) -> browser_cli.CommandResult:
    return browser_cli.CommandResult(returncode, json.dumps({"success": True, "data": {"snapshot": text}}), "")


@pytest.mark.asyncio
@pytest.mark.parametrize("shown", [
    f'- StaticText "Your key is {VALUE}"',
    f'- textbox "Key" [ref=e3]: {VALUE}',
    f'- link "Copy" [ref=e4]: /copy?k={quote(VALUE, safe="")}',
])
async def test_a_screenshot_of_a_later_page_that_shows_a_saved_value_is_refused(monkeypatch, shown):
    """Text, a field's value or an encoded copy: any form blanking hides refuses the screenshot before it is taken."""
    monkeypatch.setattr(browser_output_blanking, "_saved_values", lambda: frozenset({VALUE}))
    browser_saved_fills.mark_saved_fill(THREAD, "session-1", FILLED)
    ran: list[tuple[str, ...]] = []
    with pytest.raises(BrowserSessionError) as refused:
        await _run(monkeypatch, SHOT, visit=OTHER, page=_snapshot(f'- heading "Saved" [level=1]\n{shown}'), ran=ran)
    assert str(refused.value) == browser_saved_fills.SCREENSHOT_SHOWS_SAVED_VALUE
    assert VALUE not in str(refused.value) and ("snapshot",) in ran and SHOT not in ran


@pytest.mark.asyncio
async def test_a_screenshot_of_a_later_page_without_a_saved_value_runs(monkeypatch):
    monkeypatch.setattr(browser_output_blanking, "_saved_values", lambda: frozenset({VALUE}))
    browser_saved_fills.mark_saved_fill(THREAD, "session-1", FILLED)
    ran: list[tuple[str, ...]] = []
    page = _snapshot('- heading "Saved" [level=1]\n- StaticText "Your key was saved."')
    assert await _run(monkeypatch, SHOT, visit=OTHER, page=page, ran=ran) == "ok"
    assert ran[-2:] == [("snapshot",), SHOT]


def _unreadable():
    raise OSError("values file locked")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["values", "snapshot", "command"])
async def test_a_screenshot_whose_page_or_saved_values_cant_be_checked_is_refused(monkeypatch, failure):
    browser_saved_fills.mark_saved_fill(THREAD, "session-1", FILLED)
    page = _snapshot('- StaticText "nothing here"')
    if failure == "values":
        monkeypatch.setattr(browser_output_blanking, "_saved_values", _unreadable)
    elif failure == "snapshot":
        page = _snapshot("", returncode=1)
    ran: list[tuple[str, ...]] = []
    command = browser_saved_fills.refuse_screenshot_showing_value
    if failure == "command":
        async def broken(_args, _timeout):
            raise BrowserSessionError(browser_cli.BROWSER_BUSY)

        with pytest.raises(BrowserSessionError) as refused:
            await command(THREAD, "session-1", SHOT, broken, 5)
    else:
        with pytest.raises(BrowserSessionError) as refused:
            await _run(monkeypatch, SHOT, visit=OTHER, page=page, ran=ran)
        assert SHOT not in ran
    assert str(refused.value) == browser_saved_fills.SCREENSHOT_SHOWS_SAVED_VALUE


@pytest.mark.asyncio
async def test_without_a_saved_fill_a_screenshot_takes_no_snapshot_first(monkeypatch):
    monkeypatch.setattr(browser_output_blanking, "_saved_values", _unreadable)
    ran: list[tuple[str, ...]] = []
    assert await _run(monkeypatch, SHOT, visit=OTHER, ran=ran) == "ok"
    assert ("snapshot",) not in ran


@pytest.mark.asyncio
async def test_the_check_runs_only_for_a_screenshot(monkeypatch):
    monkeypatch.setattr(browser_output_blanking, "_saved_values", _unreadable)
    browser_saved_fills.mark_saved_fill(THREAD, "session-1", FILLED)
    ran: list[tuple[str, ...]] = []
    await _run(monkeypatch, ("get", "title"), visit=OTHER, ran=ran)
    assert ("snapshot",) not in ran


def test_the_screenshot_line_names_both_kinds_and_the_way_on():
    line = browser_saved_fills.SCREENSHOT_SHOWS_SAVED_VALUE
    assert "saved login's or saved item's value" in line and line.endswith("it shows saved values hidden.")
