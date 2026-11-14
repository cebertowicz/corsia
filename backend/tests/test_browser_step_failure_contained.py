"""A sign-in step that can't be written never breaks the browser: the command runs, the agent is told in one line,
the failure is logged with its exception, and a live step is kept for the next call or re-probe to recover."""
from __future__ import annotations

import logging
import sqlite3

import pytest

from host_access import browser_cdp, browser_control, browser_step_lifecycle as lifecycle
from host_access.browser_session_state import BrowserSessionError
from plans.errors import PlanError
from tests.browser_process_fixture import synthetic_browser  # noqa: F401
from tests.test_browser_person_step import THREAD, Page, plan  # noqa: F401

NOTE = browser_control.STEP_NOT_RECORDED


def _fail_write(monkeypatch, operation: str) -> None:
    real = lifecycle._plan_write

    def write(op, *args, **kwargs):
        if op == operation:
            raise sqlite3.OperationalError("database is locked")
        return real(op, *args, **kwargs)

    monkeypatch.setattr(lifecycle, "_plan_write", write)


def _step_logs(caplog):
    return [record for record in caplog.records if record.name == browser_control.__name__]


def _fail_row_read(monkeypatch) -> None:
    def gone(_node_id):
        raise PlanError("unknown plan node: gone")

    monkeypatch.setattr(lifecycle, "_step_row_state", gone)


async def _open_for_real(page: Page) -> None:
    page.show("visit-pw", frozenset({42}))
    with pytest.raises(BrowserSessionError, match="asks for a password"):
        await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    assert lifecycle.has_step(THREAD)


async def _three_commands(page: Page) -> list[str | None]:
    return [await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",)) for _ in range(3)]


@pytest.mark.asyncio
async def test_a_failed_row_open_does_not_stop_later_commands(plan, monkeypatch, caplog):  # noqa: F811
    page = Page()
    page.show("visit-pw", frozenset({42}))
    _fail_write(monkeypatch, "open_user_step")
    with caplog.at_level(logging.ERROR, logger=browser_control.__name__):
        notes = await _three_commands(page)
    assert notes == [NOTE] * 3
    assert not lifecycle.has_step(THREAD)
    assert len(_step_logs(caplog)) == 3
    assert all(record.exc_info and record.exc_info[0] is sqlite3.OperationalError for record in _step_logs(caplog))


@pytest.mark.asyncio
async def test_a_failed_row_read_keeps_the_live_step_and_the_next_call_recovers_it(plan, monkeypatch, caplog):  # noqa: F811
    page = Page()
    await _open_for_real(page)
    step = lifecycle._steps[THREAD]
    original = lifecycle._step_row_state
    _fail_row_read(monkeypatch)
    with caplog.at_level(logging.ERROR, logger=browser_control.__name__):
        notes = await _three_commands(page)
    assert notes == [NOTE] * 3
    assert lifecycle._steps[THREAD] is step
    assert _step_logs(caplog)[0].exc_info[0] is PlanError
    monkeypatch.setattr(lifecycle, "_step_row_state", original)
    with pytest.raises(BrowserSessionError, match="person's to complete"):
        await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    assert lifecycle._steps[THREAD] is step


@pytest.mark.asyncio
async def test_a_failed_settle_keeps_the_live_step_and_the_next_call_settles_it(plan, monkeypatch, caplog):  # noqa: F811
    page = Page()
    await _open_for_real(page)
    step = lifecycle._steps[THREAD]
    page.show("visit-next")  # the person signed in, so the step should settle
    real = lifecycle._plan_write
    _fail_write(monkeypatch, "settle_user_step")
    with caplog.at_level(logging.ERROR, logger=browser_control.__name__):
        notes = await _three_commands(page)
    assert notes == [NOTE] * 3
    assert lifecycle._steps[THREAD] is step
    assert _step_logs(caplog)[0].exc_info[0] is sqlite3.OperationalError
    monkeypatch.setattr(lifecycle, "_plan_write", real)
    assert await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",)) is None
    assert not lifecycle.has_step(THREAD)


@pytest.mark.asyncio
async def test_a_password_page_stays_read_guarded_when_its_step_cannot_be_recorded(plan, monkeypatch):  # noqa: F811
    page = Page()
    page.show("visit-pw", frozenset({42}))
    _fail_write(monkeypatch, "open_user_step")
    await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    assert browser_control._guarded_visits[THREAD]["visit-pw"].seen_nodes == frozenset({42})


@pytest.mark.asyncio
async def test_a_declaration_that_cannot_be_recorded_says_so_instead_of_claiming_a_step(plan, monkeypatch):  # noqa: F811
    page = Page()
    _fail_write(monkeypatch, "open_user_step")
    with pytest.raises(BrowserSessionError, match="couldn't be recorded"):
        await browser_control.declare_person_step(THREAD, page.agent_probe, "Choose your account")
    assert not lifecycle.has_step(THREAD)


@pytest.mark.asyncio
async def test_the_command_result_carries_the_note_after_the_command_ran(synthetic_browser, monkeypatch):  # noqa: F811
    fake = synthetic_browser
    await fake.owner.run("thread", ["snapshot"])  # the first result carries a one-time profile notice
    plain = await fake.owner.run("thread", ["snapshot"])
    # The step belongs to this session, so the lifecycle reads its row instead of ending it as a replaced session.
    session = fake.owner._sessions["thread"].record.session
    lifecycle._steps["thread"] = lifecycle.PersonStep("visit", "example.test", "target", instruction="x", node_id="row", session=session)

    async def endpoint(_command):
        return "ws://fixture"

    async def probe(_endpoint, _known):
        return browser_cdp.PasswordProbe("visit", "example.test", frozenset(), "target", present_nodes=frozenset())

    monkeypatch.setattr(browser_cdp, "page_endpoint", endpoint)
    monkeypatch.setattr(browser_cdp, "page_password_probe", probe)
    _fail_row_read(monkeypatch)
    try:
        result = await fake.owner.run("thread", ["snapshot"])
        assert result.startswith(plain) and result.endswith(f"\n{NOTE}")
        assert lifecycle.has_step("thread")
        # browser_close clears a step whose row can't be read.
        assert await fake.owner.close_thread("thread") == "Browser session closed."
        assert not lifecycle.has_step("thread")
    finally:
        lifecycle._steps.pop("thread", None)
