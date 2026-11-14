"""A sign-in step that can't be written never breaks the browser. The failure is logged with its exception and a
live step is kept for the next call or re-probe to recover. A held step refuses every command, as an open step
would (its task exists); with no step held, a page showing a password field is refused and any other page runs
the command and the agent gets one line to pass on."""
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
UNRECORDED = "waiting for the person to sign in, and their sign-in task couldn't be recorded"


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


async def _three_refusals(page: Page, match: str = UNRECORDED) -> None:
    for _ in range(3):
        with pytest.raises(BrowserSessionError, match=match):
            await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))


@pytest.mark.asyncio
async def test_a_password_page_whose_row_cannot_be_opened_refuses_every_command(plan, monkeypatch, caplog):  # noqa: F811
    page = Page()
    page.show("visit-pw", frozenset({42}))
    _fail_write(monkeypatch, "open_user_step")
    with caplog.at_level(logging.WARNING, logger=browser_control.__name__):
        await _three_refusals(page)
    assert not lifecycle.has_step(THREAD)
    assert len(_step_logs(caplog)) == 3
    assert all(record.levelno == logging.WARNING for record in _step_logs(caplog))
    assert all(record.exc_info and record.exc_info[0] is sqlite3.OperationalError for record in _step_logs(caplog))


@pytest.mark.asyncio
async def test_a_password_page_whose_row_cannot_be_read_keeps_the_live_step_and_the_next_call_recovers_it(plan, monkeypatch, caplog):  # noqa: F811
    page = Page()
    await _open_for_real(page)
    step = lifecycle._steps[THREAD]
    original = lifecycle._step_row_state
    _fail_row_read(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=browser_control.__name__):
        # The task exists, so the refusal is the normal one and never claims the task wasn't recorded.
        await _three_refusals(page, match="asks for a password")
    assert lifecycle._steps[THREAD] is step
    assert _step_logs(caplog)[0].exc_info[0] is PlanError
    monkeypatch.setattr(lifecycle, "_step_row_state", original)
    with pytest.raises(BrowserSessionError, match="person's to complete"):
        await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    assert lifecycle._steps[THREAD] is step


@pytest.mark.asyncio
async def test_a_password_page_whose_event_fails_after_the_row_opened_refuses_as_the_task_exists(plan, monkeypatch):  # noqa: F811
    page = Page()
    page.show("visit-pw", frozenset({42}))

    async def broken(_action, _thread_id):
        raise RuntimeError("event sink down")

    monkeypatch.setattr(lifecycle, "_emit", broken)
    with pytest.raises(BrowserSessionError, match="asks for a password") as raised:
        await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    assert "couldn't be recorded" not in raised.value.args[0]
    assert lifecycle.has_step(THREAD)


@pytest.mark.asyncio
async def test_a_held_person_step_refuses_every_command_when_its_row_cannot_be_read(plan, monkeypatch, caplog):  # noqa: F811
    page = Page()
    await browser_control.declare_person_step(THREAD, page.agent_probe, "Choose your account")
    step = lifecycle._steps[THREAD]
    original = lifecycle._step_row_state
    _fail_row_read(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=browser_control.__name__):
        # No password field on the page: the held step alone makes it the person's.
        for _ in range(3):
            with pytest.raises(BrowserSessionError) as raised:
                await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
            assert raised.value.args[0] == browser_control.PERSON_STEP
    assert lifecycle._steps[THREAD] is step
    assert _step_logs(caplog)[0].exc_info[0] is PlanError
    monkeypatch.setattr(lifecycle, "_step_row_state", original)
    with pytest.raises(BrowserSessionError, match="person's to complete"):
        await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    assert lifecycle._steps[THREAD] is step


@pytest.mark.asyncio
async def test_a_held_password_step_refuses_every_command_when_its_field_is_gone_but_it_cannot_be_settled(plan, monkeypatch, caplog):  # noqa: F811
    page = Page()
    await _open_for_real(page)
    step = lifecycle._steps[THREAD]
    page.show("visit-next")  # the person signed in, so the step should settle
    real = lifecycle._plan_write
    _fail_write(monkeypatch, "settle_user_step")
    with caplog.at_level(logging.WARNING, logger=browser_control.__name__):
        await _three_refusals(page, match="asks for a password")
    assert lifecycle._steps[THREAD] is step
    assert _step_logs(caplog)[0].exc_info[0] is sqlite3.OperationalError
    monkeypatch.setattr(lifecycle, "_plan_write", real)
    assert await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",)) is None
    assert not lifecycle.has_step(THREAD)


@pytest.mark.asyncio
async def test_a_page_with_no_step_and_no_password_field_runs_the_command_when_the_event_fails(plan, monkeypatch, caplog):  # noqa: F811
    page = Page()

    async def broken(*_args, **_kwargs):
        raise RuntimeError("event sink down")

    monkeypatch.setattr(lifecycle, "step_event", broken)
    with caplog.at_level(logging.WARNING, logger=browser_control.__name__):
        notes = await _three_commands(page)
    assert notes == [NOTE] * 3
    assert not lifecycle.has_step(THREAD)
    assert _step_logs(caplog)[0].exc_info[0] is RuntimeError


@pytest.mark.asyncio
async def test_the_password_read_guard_still_holds_when_the_step_cannot_be_recorded(plan, monkeypatch):  # noqa: F811
    page = Page()
    page.show("visit-pw", frozenset({42}))
    _fail_write(monkeypatch, "open_user_step")
    with pytest.raises(BrowserSessionError, match=UNRECORDED):
        await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    assert browser_control._guarded_visits[THREAD]["visit-pw"].seen_nodes == frozenset({42})
    page.show("visit-pw", frozenset(), present=frozenset({42}))  # the field is hidden again, the guard stays
    assert await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",)) is None
    with pytest.raises(BrowserSessionError, match="stay refused"):
        await browser_control.require_page_access(THREAD, page.agent_probe, ("eval", "document.title"))


@pytest.mark.asyncio
async def test_a_declaration_that_cannot_be_recorded_never_claims_a_step_state(plan, monkeypatch):  # noqa: F811
    page = Page()
    _fail_write(monkeypatch, "open_user_step")
    with pytest.raises(BrowserSessionError, match="may not have been recorded") as raised:
        await browser_control.declare_person_step(THREAD, page.agent_probe, "Choose your account")
    assert raised.value.args[0] == browser_control.DECLARATION_NOT_RECORDED
    assert not lifecycle.has_step(THREAD)


async def _stub_page(monkeypatch) -> None:
    async def endpoint(_command):
        return "ws://fixture"

    async def probe(_endpoint, _known):
        return browser_cdp.PasswordProbe("visit", "example.test", frozenset(), "target", present_nodes=frozenset())

    monkeypatch.setattr(browser_cdp, "page_endpoint", endpoint)
    monkeypatch.setattr(browser_cdp, "page_password_probe", probe)


@pytest.mark.asyncio
async def test_the_command_result_carries_the_note_after_the_command_ran(synthetic_browser, monkeypatch):  # noqa: F811
    fake = synthetic_browser
    await fake.owner.run("thread", ["snapshot"])  # the first result carries a one-time profile notice
    plain = await fake.owner.run("thread", ["snapshot"])
    await _stub_page(monkeypatch)

    async def broken(*_args, **_kwargs):
        raise RuntimeError("event sink down")

    monkeypatch.setattr(lifecycle, "step_event", broken)
    result = await fake.owner.run("thread", ["snapshot"])
    assert result.startswith(plain) and result.endswith(f"\n{NOTE}")
    assert not lifecycle.has_step("thread")


@pytest.mark.asyncio
async def test_a_held_step_whose_row_cannot_be_read_refuses_the_command_and_browser_close_as_a_healthy_step_does(synthetic_browser, monkeypatch):  # noqa: F811
    fake = synthetic_browser
    await fake.owner.run("thread", ["snapshot"])
    # The step belongs to this session, so the lifecycle reads its row instead of ending it as a replaced session.
    session = fake.owner._sessions["thread"].record.session
    lifecycle._steps["thread"] = lifecycle.PersonStep("visit", "example.test", "target", instruction="x", node_id="row", session=session)
    await _stub_page(monkeypatch)
    _fail_row_read(monkeypatch)
    try:
        with pytest.raises(BrowserSessionError) as raised:
            await fake.owner.run("thread", ["snapshot"])
        assert raised.value.args[0] == browser_control.PERSON_STEP
        assert lifecycle.has_step("thread")
        with pytest.raises(BrowserSessionError) as closing:
            await fake.owner.close_thread("thread")
        assert closing.value.args[0] == browser_control.PERSON_STEP
        assert lifecycle.has_step("thread")
    finally:
        lifecycle._steps.pop("thread", None)
