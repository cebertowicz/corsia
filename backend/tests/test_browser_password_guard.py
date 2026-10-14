import pytest

from host_access import browser_control
from host_access.browser_cdp import PasswordProbe
from host_access.browser_session_state import BrowserSessionError


@pytest.fixture(autouse=True)
def clean_password_guard():
    browser_control._episodes.clear()
    browser_control._guarded_visits.clear()
    browser_control._probe_locks.clear()
    yield
    browser_control._episodes.clear()
    browser_control._guarded_visits.clear()
    browser_control._probe_locks.clear()


@pytest.mark.asyncio
async def test_password_episode_settles_then_keeps_eval_and_hidden_password_reads_refused(monkeypatch):
    """A hidden first-step password never becomes readable during the second step."""
    responses = iter((
        PasswordProbe("visit-one", "example.test", frozenset({42})),
        PasswordProbe("visit-one", "example.test", frozenset()),
        PasswordProbe("visit-one", "example.test", frozenset()),
        PasswordProbe("visit-one", "example.test", frozenset()),
        PasswordProbe("visit-one", "example.test", frozenset()),
        PasswordProbe("visit-one", "example.test", frozenset()),
        PasswordProbe("visit-two", "example.test", frozenset()),
    ))
    events, opened, settled = [], [], []

    async def probe(_known):
        return next(responses)

    async def open_episode(_thread_id, episode):
        episode.node_id = "password-step"
        opened.append(episode.visit)

    async def settle_episode(_thread_id, episode):
        settled.append((episode.node_id, episode.seen_nodes))

    monkeypatch.setattr(browser_control, "_open_episode", open_episode)
    monkeypatch.setattr(browser_control, "_settle_episode", settle_episode)
    monkeypatch.setattr(browser_control, "_publish_password_page", lambda _thread_id, visible: events.append(visible))

    with pytest.raises(BrowserSessionError, match="asks for a password"):
        await browser_control.require_page_access("thread", probe, ("fill", "@password", "secret"))
    assert opened == ["visit-one"]
    assert browser_control.password_page("thread")

    # The field has gone and the code field is available; ordinary fixed actions resume.
    await browser_control.require_page_access("thread", probe, ("fill", "@code", "123456"))
    assert settled == [("password-step", frozenset({42}))]
    assert events == [True, False]
    assert not browser_control.password_page("thread")

    for command in (("eval", "document.body.innerText"), ("get", "value", "42"), ("get", "attr", "42", "value")):
        with pytest.raises(BrowserSessionError, match="asks for a password"):
            await browser_control.require_page_access("thread", probe, command)

    # Snapshot does not request a password value, and a fresh navigation resets the visit guard.
    await browser_control.require_page_access("thread", probe, ("snapshot",))
    await browser_control.require_page_access("thread", probe, ("eval", "document.title"))


@pytest.mark.asyncio
async def test_password_probe_failure_fails_closed():
    async def broken_probe(_known):
        raise RuntimeError("CDP went away")

    with pytest.raises(BrowserSessionError, match="couldn't be checked"):
        await browser_control.require_page_access("thread", broken_probe, ("snapshot",))
