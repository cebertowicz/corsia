import asyncio

import pytest

from host_access import browser_cdp, browser_control
from host_access.browser_cdp import PasswordProbe
from host_access.browser_session_state import BrowserSessionError


@pytest.fixture(autouse=True)
def clean_password_guard():
    browser_control._panes.clear()
    browser_control._pane_reports.clear()
    browser_control._pane_probes.clear()
    browser_control._episodes.clear()
    browser_control._guarded_visits.clear()
    browser_control._probe_locks.clear()
    yield
    browser_control._panes.clear()
    browser_control._pane_reports.clear()
    browser_control._pane_probes.clear()
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

    async def probe(_visit, _known):
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
async def test_new_password_visit_opens_a_new_step_and_reports_to_the_pane(monkeypatch):
    """A settled visit does not suppress a later password visit's step or pane pill."""
    responses = iter((
        PasswordProbe("visit-one", "example.test", frozenset({42})),
        PasswordProbe("visit-two", "example.test", frozenset()),
        PasswordProbe("visit-three", "example.test", frozenset({43})),
    ))
    opened, settled, pane_events = [], [], []

    async def probe(_visit, _known):
        return next(responses)

    async def open_episode(_thread_id, episode):
        episode.node_id = f"step-{episode.visit}"
        opened.append(episode.visit)

    async def settle_episode(_thread_id, episode):
        settled.append(episode.visit)

    monkeypatch.setattr(browser_control, "_open_episode", open_episode)
    monkeypatch.setattr(browser_control, "_settle_episode", settle_episode)
    monkeypatch.setattr(browser_control.state, "broadcast_to_thread", lambda *_args, **_kwargs: None)
    browser_control.pane_connected("thread", "pane", pane_events.append)

    with pytest.raises(BrowserSessionError, match="asks for a password"):
        await browser_control.require_page_access("thread", probe, ("snapshot",))
    await browser_control.require_page_access("thread", probe, ("snapshot",))
    with pytest.raises(BrowserSessionError, match="asks for a password"):
        await browser_control.require_page_access("thread", probe, ("snapshot",))

    assert opened == ["visit-one", "visit-three"]
    assert settled == ["visit-one"]
    assert [event["visible"] for event in pane_events] == [True, False, True]


@pytest.mark.asyncio
async def test_password_probe_keeps_known_nodes_only_on_their_own_visit(monkeypatch):
    """A node seen on the password page can still resolve to a box after the page navigates away."""
    class FakeCdp:
        def __init__(self, _endpoint):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def call(self, method, **_params):
            return {
                "Page.getFrameTree": {"frameTree": {"frame": {"loaderId": "visit-two", "url": "https://example.test/next"}}},
                "DOM.getDocument": {"root": {"nodeId": 1}},
                "DOM.querySelectorAll": {"nodeIds": []},
                "DOM.getBoxModel": {"model": {"border": [0, 0, 10, 0, 10, 10, 0, 10]}},
            }[method]

    async def endpoint(_run):
        return "ws://page.test"

    monkeypatch.setattr(browser_cdp, "CdpClient", FakeCdp)
    monkeypatch.setattr(browser_cdp, "_endpoint", endpoint)
    assert (await browser_cdp.password_probe(None, "visit-one", frozenset({42}))).visible_nodes == frozenset()
    # Within one visit a known field stays guarded even after a reveal toggle changes its type.
    assert (await browser_cdp.password_probe(None, "visit-two", frozenset({42}))).visible_nodes == frozenset({42})


@pytest.mark.asyncio
async def test_password_probe_failure_fails_closed():
    async def broken_probe(_visit, _known):
        raise RuntimeError("CDP went away")

    with pytest.raises(BrowserSessionError, match="couldn't be checked"):
        await browser_control.require_page_access("thread", broken_probe, ("snapshot",))


class SignInPage:
    """A single-document sign-in page whose password field hides once the person submits it."""

    def __init__(self, failures=0):
        self.submitted = False
        self.failures = failures
        self.agent_probes = 0
        self.pane_probes = 0
        self.probed = asyncio.Event()

    def _probe(self):
        return PasswordProbe("visit-one", "example.test", frozenset() if self.submitted else frozenset({42}))

    async def agent_probe(self, _visit, _known):
        self.agent_probes += 1
        return self._probe()

    async def pane_probe(self, _visit, _known):
        self.pane_probes += 1
        self.probed.set()
        if self.failures:
            self.failures -= 1
            raise BrowserSessionError("This page couldn't be checked")
        return self._probe()


@pytest.fixture
def steps(monkeypatch):
    """Record the person's step opening and settling; the step stays open unless a test ends it."""
    recorded = {"opened": [], "settled": [], "ended": False}

    async def open_episode(_thread_id, episode):
        episode.node_id = "password-step"
        recorded["opened"].append(episode.visit)

    async def settle_episode(_thread_id, episode):
        recorded["settled"].append(episode.node_id)

    monkeypatch.setattr(browser_control, "_open_episode", open_episode)
    monkeypatch.setattr(browser_control, "_settle_episode", settle_episode)
    monkeypatch.setattr(browser_control, "_step_ended", lambda _episode: recorded["ended"])
    monkeypatch.setattr(browser_control, "_publish_password_page", lambda _thread_id, _visible: None)
    return recorded


async def _agent_meets_password_page(page):
    browser_control.pane_connected("thread", "pane", None, page.pane_probe)
    with pytest.raises(BrowserSessionError, match="asks for a password"):
        await browser_control.require_page_access("thread", page.agent_probe, ("snapshot",))
    episode = browser_control._episodes["thread"]
    assert episode.reprobe is not None
    return episode, episode.reprobe


async def _probed(page, count):
    while page.pane_probes < count:
        page.probed.clear()
        await page.probed.wait()


@pytest.mark.asyncio
async def test_a_single_document_sign_in_settles_after_the_person_types_with_no_agent_command(steps):
    page = SignInPage()
    episode, loop = await _agent_meets_password_page(page)
    clock = asyncio.get_running_loop().time
    for _ in range(3):
        browser_control.person_input("thread")
    # The person presses Enter and the page hides its field without navigating.
    page.submitted = True
    typed = clock()
    browser_control.person_input("thread")
    await asyncio.wait_for(loop, 2.5)
    assert clock() - typed >= browser_control.REPROBE_AFTER_INPUT
    assert steps["settled"] == ["password-step"]
    assert not browser_control.password_page("thread")
    assert page.agent_probes == 1
    # The loop ended itself after settling; nothing is left running.
    assert loop.done() and not loop.cancelled() and episode.reprobe is None


@pytest.mark.asyncio
async def test_the_reprobe_keeps_the_episode_open_while_the_field_shows_and_stops_when_the_pane_leaves(steps, monkeypatch):
    monkeypatch.setattr(browser_control, "REPROBE_EVERY", 0.01)
    page = SignInPage()
    episode, loop = await _agent_meets_password_page(page)
    await asyncio.wait_for(_probed(page, 3), 1)
    assert browser_control.password_page("thread") and steps["settled"] == []
    browser_control.pane_disconnected("thread", "pane")
    await asyncio.gather(loop, return_exceptions=True)
    assert loop.cancelled() and episode.reprobe is None
    # The step stays open; the agent's next command probes again.
    assert browser_control.password_page("thread")
    # A pane that comes back resumes the episode's re-probe, and a closed browser stops it.
    browser_control.pane_connected("thread", "pane", None, page.pane_probe)
    resumed = episode.reprobe
    assert resumed is not None and resumed is not loop
    browser_control.browser_closed("thread")
    await asyncio.gather(resumed, return_exceptions=True)
    assert resumed.cancelled() and episode.reprobe is None


@pytest.mark.asyncio
async def test_the_reprobe_stops_without_probing_once_the_person_declines_the_step(steps, monkeypatch):
    monkeypatch.setattr(browser_control, "REPROBE_EVERY", 0.01)
    page = SignInPage()
    _episode, loop = await _agent_meets_password_page(page)
    steps["ended"] = True
    await asyncio.wait_for(loop, 1)
    assert page.pane_probes == 0 and steps["settled"] == []
    # AR7: the page stays guarded while its field shows.
    assert browser_control.password_page("thread")


@pytest.mark.asyncio
async def test_an_agent_command_that_settles_the_episode_stops_its_reprobe(steps):
    page = SignInPage()
    episode, loop = await _agent_meets_password_page(page)
    page.submitted = True
    await browser_control.require_page_access("thread", page.agent_probe, ("snapshot",))
    await asyncio.gather(loop, return_exceptions=True)
    assert loop.cancelled() and episode.reprobe is None
    assert steps["settled"] == ["password-step"]


@pytest.mark.asyncio
async def test_a_failed_reprobe_keeps_the_episode_open_until_a_probe_succeeds(steps, monkeypatch):
    monkeypatch.setattr(browser_control, "REPROBE_EVERY", 0.01)
    page = SignInPage(failures=2)
    page.submitted = True
    _episode, loop = await _agent_meets_password_page(SignInPage())
    browser_control.pane_connected("thread", "pane", None, page.pane_probe)
    await asyncio.wait_for(loop, 1)
    assert page.pane_probes == 3
    assert steps["settled"] == ["password-step"]
