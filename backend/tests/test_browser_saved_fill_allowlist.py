"""After a saved login is filled, the session runs under the filled-field allowlist until it ends.

`mark_saved_fill` is what the fill executor calls before it types. These tests drive the page guard the
agent's commands go through (`check_page_access`) on a page with no password field showing, so the only
reason for a refusal is the session's mark.
"""
from __future__ import annotations

import pytest

from host_access import browser_control, browser_step_lifecycle as lifecycle
from host_access.browser_cdp import PasswordProbe
from host_access.browser_session_state import BrowserSessionError

THREAD = "thread-saved-fill"


@pytest.fixture(autouse=True)
def _fresh():
    for held in (lifecycle._steps, lifecycle._answers, lifecycle._sessions, lifecycle._probe_locks,
                 browser_control._guarded_visits, browser_control._saved_fill_sessions):
        held.clear()
    yield
    for held in (lifecycle._steps, lifecycle._answers, lifecycle._sessions, lifecycle._probe_locks,
                 browser_control._guarded_visits, browser_control._saved_fill_sessions):
        held.clear()


async def _no_password_field(_known):
    return PasswordProbe("loader-1", "example.com", frozenset(), "target-1", present_nodes=frozenset())


async def _access(args: tuple[str, ...], session: str = "session-1") -> browser_control.PageAccess:
    async def resolve(_target):
        return 7

    return await browser_control.check_page_access(THREAD, _no_password_field, args, resolve=resolve, session=session)


REFUSED = [
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
ALLOWED = [
    ("snapshot",), ("snapshot", "-i"), ("click", "@e2"), ("fill", "@e3", "text"), ("get", "text", "@e1"),
    ("get", "url"), ("get", "title"), ("open", "https://example.com/next"), ("reload",), ("wait", "500"),
    ("press", "Enter"), ("tab",),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("args", REFUSED)
async def test_after_a_saved_login_fill_scripts_value_reads_routes_and_screenshots_are_refused(args):
    browser_control.mark_saved_fill(THREAD, "session-1")
    with pytest.raises(BrowserSessionError) as refused:
        await _access(args)
    assert str(refused.value) == browser_control.FILLED_FIELD_REFUSED


@pytest.mark.asyncio
@pytest.mark.parametrize("args", ALLOWED)
async def test_after_a_saved_login_fill_commands_that_cant_show_it_still_run_with_snapshot_values_hidden(args):
    browser_control.mark_saved_fill(THREAD, "session-1")
    access = await _access(args)
    # filled_field_visit is what makes the command path hide a snapshot's field values.
    assert access.filled_field_visit is True


@pytest.mark.asyncio
async def test_the_allowlist_holds_on_every_page_of_the_session():
    browser_control.mark_saved_fill(THREAD, "session-1")

    async def another_page(_known):
        return PasswordProbe("loader-2", "other.example", frozenset(), "target-1", present_nodes=frozenset())

    with pytest.raises(BrowserSessionError):
        await browser_control.check_page_access(THREAD, another_page, ("eval", "1"), session="session-1")


@pytest.mark.asyncio
async def test_without_a_fill_or_in_a_later_session_the_allowlist_does_not_apply():
    assert (await _access(("eval", "1"))).filled_field_visit is False
    browser_control.mark_saved_fill(THREAD, "session-1")
    browser_control.browser_closed(THREAD)
    assert (await _access(("eval", "1"), session="session-2")).filled_field_visit is False


@pytest.mark.asyncio
async def test_a_mark_for_one_session_does_not_reach_another():
    browser_control.mark_saved_fill(THREAD, "session-1")
    assert (await _access(("eval", "1"), session="session-2")).filled_field_visit is False
