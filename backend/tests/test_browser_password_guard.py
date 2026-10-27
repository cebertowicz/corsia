import asyncio

import pytest

from host_access import browser_cdp, browser_control, browser_pane_state, browser_steps, browser_step_lifecycle as lifecycle
from host_access.browser_cdp import PasswordProbe
from host_access.browser_session_state import BrowserSessionError


@pytest.fixture(autouse=True)
def clean_password_guard(monkeypatch):
    monkeypatch.setattr(lifecycle, "_step_row_state", lambda _node_id: None)
    browser_pane_state._panes.clear()
    browser_pane_state._pane_reports.clear()
    browser_pane_state._pane_probes.clear()
    lifecycle._steps.clear()
    lifecycle._answers.clear()
    lifecycle._sessions.clear()
    browser_control._guarded_visits.clear()
    lifecycle._probe_locks.clear()
    browser_pane_state._agent_seen.clear()
    yield
    browser_pane_state._panes.clear()
    browser_pane_state._pane_reports.clear()
    browser_pane_state._pane_probes.clear()
    lifecycle._steps.clear()
    lifecycle._answers.clear()
    lifecycle._sessions.clear()
    browser_control._guarded_visits.clear()
    lifecycle._probe_locks.clear()
    browser_pane_state._agent_seen.clear()


@pytest.mark.asyncio
async def test_password_step_settles_then_keeps_eval_and_hidden_password_reads_refused(monkeypatch):
    """A hidden first-step password never becomes readable during the second step."""
    responses = iter((
        PasswordProbe("visit-one", "example.test", frozenset({42}), present_nodes=frozenset({42})),
        PasswordProbe("visit-one", "example.test", frozenset(), present_nodes=frozenset({42})),
        PasswordProbe("visit-one", "example.test", frozenset(), present_nodes=frozenset({42})),
        PasswordProbe("visit-one", "example.test", frozenset(), present_nodes=frozenset({42})),
        PasswordProbe("visit-one", "example.test", frozenset(), present_nodes=frozenset({42})),
        PasswordProbe("visit-one", "example.test", frozenset(), present_nodes=frozenset({42})),
        PasswordProbe("visit-two", "example.test", frozenset(), present_nodes=frozenset()),
    ))
    events, opened, settled = [], [], []

    async def probe(_known):
        return next(responses)

    async def open_step(_thread_id, step):
        lifecycle._steps[_thread_id] = step
        lifecycle._start_reprobe(_thread_id, step)
        step.node_id = "password-step"
        opened.append(step.visit)

    async def settle_step(_thread_id, step):
        settled.append((step.node_id, browser_control._guarded_visits[_thread_id][step.visit].seen_nodes))

    monkeypatch.setattr(lifecycle, "_open_step", open_step)
    monkeypatch.setattr(lifecycle, "_settle_step", settle_step)
    monkeypatch.setattr(browser_steps, "publish_password_page", lambda _thread_id, visible: events.append(visible))

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

    # Snapshot does not request a password value, and a fresh document has no guard of its own.
    await browser_control.require_page_access("thread", probe, ("snapshot",))
    await browser_control.require_page_access("thread", probe, ("eval", "document.title"))


@pytest.mark.asyncio
async def test_new_password_visit_opens_a_new_step_and_reports_to_the_pane(monkeypatch):
    """A settled visit does not suppress a later password visit's step or pane pill."""
    responses = iter((
        PasswordProbe("visit-one", "example.test", frozenset({42}), present_nodes=frozenset({42})),
        PasswordProbe("visit-two", "example.test", frozenset(), present_nodes=frozenset()),
        PasswordProbe("visit-three", "example.test", frozenset({43}), present_nodes=frozenset({43})),
    ))
    opened, settled, pane_events = [], [], []

    async def probe(_known):
        return next(responses)

    async def open_step(_thread_id, step):
        lifecycle._steps[_thread_id] = step
        lifecycle._start_reprobe(_thread_id, step)
        step.node_id = f"step-{step.visit}"
        opened.append(step.visit)

    async def settle_step(_thread_id, step):
        settled.append(step.visit)

    monkeypatch.setattr(lifecycle, "_open_step", open_step)
    monkeypatch.setattr(lifecycle, "_settle_step", settle_step)
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
                "DOM.performSearch": {"searchId": "search", "resultCount": 0},
                "DOM.discardSearchResults": {},
                "DOM.getBoxModel": {"model": {"border": [0, 0, 10, 0, 10, 10, 0, 10]}},
            }[method]

    monkeypatch.setattr(browser_cdp, "CdpClient", FakeCdp)
    assert (await browser_cdp.page_password_probe("ws://page.test", {"visit-one": frozenset({42})})).visible_nodes == frozenset()
    # Within one visit a known field stays guarded even after a reveal toggle changes its type.
    probe = await browser_cdp.page_password_probe("ws://page.test", {"visit-two": frozenset({42})})
    assert probe.visible_nodes == frozenset({42}) and probe.target == "ws://page.test"


@pytest.mark.asyncio
@pytest.mark.parametrize("visible", [True, False], ids=["visible", "hidden"])
async def test_password_probe_reads_only_search_matches_and_keeps_only_password_inputs(monkeypatch, visible):
    """AR3: the probe never transfers the whole DOM and discards its search when done."""
    calls = []
    nodes = {
        5: {"nodeName": "INPUT", "backendNodeId": 77,
            "attributes": ["type", "password", "value", "private-fixture-value"]},
        6: {"nodeName": "LABEL", "backendNodeId": 78, "attributes": ["for", "password"]},
    }

    class FakeCdp:
        def __init__(self, _endpoint):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def call(self, method, **params):
            calls.append((method, params))
            if method == "DOM.describeNode":
                return {"node": nodes[params["nodeId"]]}
            return {
                "Page.getFrameTree": {"frameTree": {"frame": {"loaderId": "visit", "url": "https://accounts.example.co.uk/"}}},
                "DOM.getDocument": {"root": {"nodeId": 1}},
                "DOM.performSearch": {"searchId": "search", "resultCount": 2},
                "DOM.getSearchResults": {"nodeIds": [5, 6]},
                "DOM.discardSearchResults": {},
                "DOM.getBoxModel": {"model": {"border": [0, 0, 10, 0, 10, 10, 0, 10] if visible else [0] * 8}},
            }[method]

    monkeypatch.setattr(browser_cdp, "CdpClient", FakeCdp)
    probe = await browser_cdp.page_password_probe("ws://page.test", {})
    assert probe.visible_nodes == (frozenset({77}) if visible else frozenset())
    assert probe.present_nodes == frozenset({77}) and probe.host == "accounts.example.co.uk"
    assert set(vars(probe)) == {"visit", "host", "target", "visible_nodes", "present_nodes"}
    assert "private-fixture-value" not in repr(probe)
    assert ("DOM.getDocument", {"depth": 0}) in calls
    assert not any(method == "DOM.getDocument" and params.get("depth") == -1 for method, params in calls)
    assert ("DOM.discardSearchResults", {"searchId": "search"}) in calls


@pytest.mark.asyncio
async def test_a_hidden_password_read_is_refused_by_the_node_its_ref_or_selector_names(monkeypatch):
    """AR2: a read is refused when its @ref or CSS selector locates a seen password node, or can't be located."""
    responses = iter((PasswordProbe("visit", "example.test", frozenset({42}), present_nodes=frozenset({42})),))
    hidden = PasswordProbe("visit", "example.test", frozenset(), present_nodes=frozenset({42}))

    async def probe(_known):
        return next(responses, hidden)

    async def open_step(_thread_id, step):
        lifecycle._steps[_thread_id] = step
        lifecycle._start_reprobe(_thread_id, step)
        step.node_id = "password-step"

    async def settle_step(_thread_id, _step):
        pass

    monkeypatch.setattr(lifecycle, "_open_step", open_step)
    monkeypatch.setattr(lifecycle, "_settle_step", settle_step)
    monkeypatch.setattr(browser_steps, "publish_password_page", lambda *_args: None)
    with pytest.raises(BrowserSessionError, match="asks for a password"):
        await browser_control.require_page_access("thread", probe, ("snapshot",))
    await browser_control.require_page_access("thread", probe, ("snapshot",))

    located = {"@e3": 42, "#password": 42, "@e4": 7, "#gone": None}

    async def resolve(selector):
        if selector == "@broken":
            raise BrowserSessionError("This page couldn't be checked")
        return located[selector]

    for selector in ("@e3", "#password", "#gone", "@broken"):
        with pytest.raises(BrowserSessionError, match="asks for a password"):
            await browser_control.require_page_access("thread", probe, ("get", "value", selector), resolve=resolve)
    await browser_control.require_page_access("thread", probe, ("get", "value", "@e4"), resolve=resolve)
    await browser_control.require_page_access("thread", probe, ("get", "attr", "@e4", "href"), resolve=resolve)


@pytest.mark.asyncio
async def test_password_probe_failure_fails_closed():
    async def broken_probe(_known):
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
        return PasswordProbe("visit-one", "example.test", frozenset() if self.submitted else frozenset({42}),
                             present_nodes=frozenset({42}))

    async def agent_probe(self, _known):
        self.agent_probes += 1
        return self._probe()

    async def pane_probe(self, _known):
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

    async def open_step(_thread_id, step):
        lifecycle._steps[_thread_id] = step
        lifecycle._start_reprobe(_thread_id, step)
        step.node_id = "password-step"
        recorded["opened"].append(step.visit)

    async def settle_step(_thread_id, step):
        recorded["settled"].append(step.node_id)

    monkeypatch.setattr(lifecycle, "_open_step", open_step)
    monkeypatch.setattr(lifecycle, "_settle_step", settle_step)

    async def emit(action, _thread_id):
        recorded["events"].append(action)

    recorded["events"] = []
    # The step's plan row reads as closed by the person once a test sets ``ended``.
    monkeypatch.setattr(lifecycle, "_step_row_state", lambda _node_id: "closed" if recorded["ended"] else None)
    monkeypatch.setattr(lifecycle, "_emit", emit)
    monkeypatch.setattr(browser_steps, "publish_password_page", lambda _thread_id, _visible: None)
    return recorded


async def _agent_meets_password_page(page):
    browser_control.pane_connected("thread", "pane", None, page.pane_probe)
    with pytest.raises(BrowserSessionError, match="asks for a password"):
        await browser_control.require_page_access("thread", page.agent_probe, ("snapshot",))
    step = lifecycle._steps["thread"]
    assert step.reprobe is not None
    return step, step.reprobe


async def _probed(page, count):
    while page.pane_probes < count:
        page.probed.clear()
        await page.probed.wait()


@pytest.mark.asyncio
async def test_a_single_document_sign_in_settles_after_the_person_types_with_no_agent_command(steps):
    page = SignInPage()
    step, loop = await _agent_meets_password_page(page)
    clock = asyncio.get_running_loop().time
    for _ in range(3):
        browser_control.person_input("thread")
    # The person presses Enter and the page hides its field without navigating.
    page.submitted = True
    typed = clock()
    browser_control.person_input("thread")
    await asyncio.wait_for(loop, 2.5)
    assert clock() - typed >= lifecycle.REPROBE_AFTER_INPUT
    assert steps["settled"] == ["password-step"]
    assert not browser_control.password_page("thread")
    assert page.agent_probes == 1
    # The loop ended itself after settling; nothing is left running.
    assert loop.done() and not loop.cancelled() and step.reprobe is None


@pytest.mark.asyncio
async def test_the_reprobe_keeps_the_step_open_while_the_field_shows_and_stops_when_the_pane_leaves(steps, monkeypatch):
    monkeypatch.setattr(lifecycle, "REPROBE_EVERY", 0.01)
    page = SignInPage()
    step, loop = await _agent_meets_password_page(page)
    await asyncio.wait_for(_probed(page, 3), 1)
    assert browser_control.password_page("thread") and steps["settled"] == []
    browser_control.pane_disconnected("thread", "pane")
    await asyncio.gather(loop, return_exceptions=True)
    assert loop.cancelled() and step.reprobe is None
    # The step stays open; the agent's next command probes again.
    assert browser_control.password_page("thread")
    # A pane that comes back resumes the step's re-probe, and a closed browser stops it.
    browser_control.pane_connected("thread", "pane", None, page.pane_probe)
    await asyncio.sleep(0)
    resumed = step.reprobe
    assert resumed is not None and resumed is not loop
    await lifecycle.step_event("thread", lifecycle.Event("session_ended"))
    await asyncio.gather(resumed, return_exceptions=True)
    assert resumed.cancelled() and step.reprobe is None


@pytest.mark.asyncio
async def test_the_shared_reprobe_ends_a_declined_step_and_never_resumes_it(steps, monkeypatch):
    monkeypatch.setattr(lifecycle, "REPROBE_EVERY", 0.01)
    page = SignInPage()
    _step, loop = await _agent_meets_password_page(page)
    steps["ended"] = True
    await asyncio.wait_for(loop, 1)
    assert page.pane_probes == 1 and steps["settled"] == []
    assert steps["events"] == ["password_step_declined"]
    assert not browser_control.password_page("thread")
    assert browser_control._guarded_visits["thread"]["visit-one"].seen_nodes == frozenset({42})
    # A pane that reconnects resumes nothing and never counts the decline twice.
    browser_control.pane_disconnected("thread", "pane")
    browser_control.pane_connected("thread", "pane", None, page.pane_probe)
    await asyncio.sleep(0)
    assert "thread" not in lifecycle._steps
    assert steps["events"] == ["password_step_declined"]


@pytest.mark.asyncio
async def test_a_password_step_names_the_registrable_domain_and_reports_each_transition(monkeypatch):
    """AR5: a lookalike host's step names the site that serves it; opening and settling are product events."""
    writes, events = [], []

    def plan_write(operation, *args, **kwargs):
        writes.append((operation, args, kwargs))
        return "password-step" if operation == "open_user_step" else None

    monkeypatch.setattr(lifecycle, "_plan_write", plan_write)
    monkeypatch.setattr(lifecycle, "emit_product_event",
                        lambda surface, action, **kwargs: events.append((surface, action, kwargs["thread_id"])))
    step = browser_steps.PasswordStep("visit", "paypal.com.evil.com.br")
    await lifecycle._open_step("thread", step)
    await lifecycle._settle_step("thread", step)
    assert writes[0][0] == "open_user_step"
    assert writes[0][2]["title"] == "Enter your password in the shared browser (evil.com.br)"
    assert writes[1][:2] == ("settle_user_step", ("password-step",))
    assert events == [("browser_pane", "password_step_opened", "thread"),
                      ("browser_pane", "password_step_settled", "thread")]


@pytest.mark.asyncio
async def test_an_agent_command_that_settles_the_step_stops_its_reprobe(steps):
    page = SignInPage()
    step, loop = await _agent_meets_password_page(page)
    page.submitted = True
    await browser_control.require_page_access("thread", page.agent_probe, ("snapshot",))
    await asyncio.gather(loop, return_exceptions=True)
    assert loop.cancelled() and step.reprobe is None
    assert steps["settled"] == ["password-step"]


@pytest.mark.asyncio
async def test_a_failed_reprobe_keeps_the_step_open_until_a_probe_succeeds(steps, monkeypatch):
    monkeypatch.setattr(lifecycle, "REPROBE_EVERY", 0.01)
    page = SignInPage(failures=2)
    page.submitted = True
    _step, loop = await _agent_meets_password_page(SignInPage())
    browser_control.pane_connected("thread", "pane", None, page.pane_probe)
    await asyncio.wait_for(loop, 1)
    assert page.pane_probes == 3
    assert steps["settled"] == ["password-step"]
