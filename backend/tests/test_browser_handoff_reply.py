"""A page handed to the person tells the agent what the person can see: the page in an open pane, else only its task."""
import pytest

from host_access import browser_control
from host_access.browser_session_state import BrowserSessionError
from tests.test_browser_person_step import THREAD, Page, _steps, plan  # noqa: F401 - plan is a fixture


async def _refusal(page: Page) -> str:
    with pytest.raises(BrowserSessionError) as refused:
        await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    return str(refused.value)


def _open_pane(page: Page) -> None:
    browser_control.pane_connected(THREAD, "pane", None, page.pane_probe)


@pytest.mark.parametrize("pane", [False, True], ids=["no-pane", "pane-open"])
@pytest.mark.asyncio
async def test_password_page_reply_says_whether_the_person_sees_the_page(plan, pane):
    page = Page()
    if pane:
        _open_pane(page)
    page.show("visit-one", frozenset({42}))
    message = await _refusal(page)
    assert message.startswith(browser_control.PASSWORD_PAGE)
    assert len(_steps(plan["db"])) == 1  # the Needs you task the reply names exists
    if pane:
        assert message == browser_control.PASSWORD_PAGE + browser_control.PANE_OPEN + " Post nothing in chat."
    else:
        assert browser_control.NO_PANE in message
        assert browser_control.PANE_OPEN not in message
        assert "notice" not in message
        assert "pointing them to it in Needs you" in message


@pytest.mark.parametrize("pane", [False, True], ids=["no-pane", "pane-open"])
@pytest.mark.asyncio
async def test_person_step_reply_says_whether_the_person_sees_the_page(plan, pane):
    page = Page()
    if pane:
        _open_pane(page)
    await browser_control.declare_person_step(THREAD, page.agent_probe, "Choose your account")
    assert len(_steps(plan["db"])) == 1
    seen = browser_control.PANE_OPEN if pane else browser_control.NO_PANE
    assert await _refusal(page) == browser_control.PERSON_STEP + seen


@pytest.mark.asyncio
async def test_reply_follows_the_pane_closing_while_the_step_waits(plan):
    page = Page()
    _open_pane(page)
    page.show("visit-one", frozenset({42}))
    assert browser_control.PANE_OPEN in await _refusal(page)
    browser_control.pane_disconnected(THREAD, "pane")
    assert browser_control.NO_PANE in await _refusal(page)
