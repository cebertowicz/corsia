"""Session teardown stays independent of plans and unavailable browser pages."""
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from host_access import browser_cdp, browser_control, browser_step_lifecycle as lifecycle
from host_access.browser_session_state import BrowserSessionCleanupError
from host_access.browser_steps import PersonStep
from tests.browser_process_fixture import synthetic_browser  # noqa: F401
from tests.test_browser_person_step import plan  # noqa: F401


def _open_step():
    step = PersonStep("visit", "example.test", "target", instruction="Choose your account", node_id="row")
    lifecycle._steps["thread"] = step
    return step


@pytest.mark.parametrize("failure", ["row_read", "hook"])
async def test_session_teardown_does_not_depend_on_plan_or_hook_success(plan, synthetic_browser, monkeypatch, caplog, failure):
    fake = synthetic_browser
    await fake.owner.run("thread", ["snapshot"])
    runtime = fake.owner._sessions["thread"]
    step = _open_step()

    def fail_read(_node_id):
        raise RuntimeError("plan database unavailable")

    async def fail_hook(*_args, **_kwargs):
        raise RuntimeError("session hook failed")

    monkeypatch.setattr(lifecycle, "_step_row_state", fail_read)
    if failure == "hook":
        monkeypatch.setattr(lifecycle, "step_event", fail_hook)
    await fake.owner.shutdown()
    assert runtime.record.terminal_state == "closed"
    assert fake.owner.live_count() == 0
    assert "thread" not in lifecycle._steps
    assert step.node_id == "row"
    if failure == "hook":
        assert "session hook failed" in caplog.text


@pytest.mark.parametrize("state", ["running", "resetting", "death_unverified"])
@pytest.mark.parametrize("failure", ["endpoint", "probe"])
async def test_unreachable_step_page_does_not_block_owned_close(plan, synthetic_browser, monkeypatch, state, failure):
    fake = synthetic_browser
    await fake.owner.run("thread", ["snapshot"])
    runtime = fake.owner._sessions["thread"]
    runtime.record = replace(runtime.record, terminal_state=state)
    _open_step()
    calls = []

    async def endpoint(_command):
        calls.append("endpoint")
        if failure == "endpoint":
            raise BrowserSessionCleanupError("page endpoint unavailable")
        return "ws://fixture"

    async def probe(_endpoint, _known):
        calls.append("probe")
        raise RuntimeError("page probe unavailable")

    monkeypatch.setattr(browser_cdp, "page_endpoint", endpoint)
    monkeypatch.setattr(browser_cdp, "page_password_probe", probe)
    assert await fake.owner.close_thread("thread") == "Browser session closed."
    assert runtime.record.terminal_state == "closed"
    assert "thread" not in lifecycle._steps
    if state != "running":
        assert calls == []


async def test_hook_failure_does_not_hide_teardown_failure_or_its_recovery(plan, synthetic_browser, monkeypatch):
    from host_access import browser_reporting

    owner = synthetic_browser.owner
    await owner.run("thread", ["snapshot"])
    runtime = owner._sessions["thread"]
    runtime.record = replace(runtime.record, terminal_state="resetting")
    _open_step()
    report = AsyncMock(return_value=("thread", "notice"))
    monkeypatch.setattr(browser_reporting, "report_cleanup", report)
    close = owner._request_close

    async def fail_hook(*_args, **_kwargs):
        raise RuntimeError("session hook failed")

    async def fail_close(_runtime):
        raise RuntimeError("producer close failed")

    monkeypatch.setattr(lifecycle, "step_event", fail_hook)
    monkeypatch.setattr(owner, "_request_close", fail_close)
    with pytest.raises(BrowserSessionCleanupError, match="could not be verified"):
        await owner.close_thread("thread")
    assert runtime.record.terminal_state == "death_unverified"
    report.assert_awaited_once_with("thread")
    assert "thread" not in lifecycle._steps
    monkeypatch.setattr(owner, "_request_close", close)
    await owner.close_thread("thread")
    assert runtime.record.terminal_state == "closed"


async def test_session_replacement_ignores_row_read_failure(plan, monkeypatch):
    from tests.test_browser_person_step import THREAD, Page

    page = Page()
    await browser_control.declare_person_step(THREAD, page.agent_probe, "Choose your account", session="old")
    step = lifecycle._steps[THREAD]

    def fail_read(_node_id):
        raise RuntimeError("plan database unavailable")

    monkeypatch.setattr(lifecycle, "_step_row_state", fail_read)
    await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",), session="new")
    assert THREAD not in lifecycle._steps
    assert step.node_id is not None


async def test_observer_runs_once_before_effects_and_is_optional(plan, monkeypatch):
    order = []

    def observer(result, clear_answers, guard_present):
        order.append("observer")
        assert result is None and clear_answers and not guard_present

    step = _open_step()
    monkeypatch.setattr(step, "ended", lambda _thread: order.append("ended"))
    await lifecycle.step_event("thread", lifecycle.Event("session_ended"), observer=observer)
    assert order == ["observer", "ended"]
    await lifecycle.step_event("thread", lifecycle.Event("session_ended"))
    assert order == ["observer", "ended"]
