"""The decided transition table and complete session/answer journeys with real plan writes."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from host_access import browser_cdp, browser_control, browser_step_lifecycle as lifecycle
from host_access.browser_cdp import PasswordProbe
from host_access.browser_session_state import BrowserRuntime, BrowserSessionError, BrowserSessionRecord
from host_access.browser_sessions import BrowserSessionOwner
from host_access.browser_steps import PasswordStep, PersonStep
from tests.test_browser_person_step import PAGE, THREAD, Page, _steps, plan
from tests.plan_close_next_helpers import HUMAN, close_params, plan_update, row


def _probe(visit="visit-one", visible=False, target=PAGE):
    return PasswordProbe(visit, "accounts.example.test", frozenset({42}) if visible else frozenset(), target,
                         present_nodes=frozenset({42}) if visible else frozenset())


def _person():
    return PersonStep("visit-one", "accounts.example.test", PAGE, session="session-one", instruction="Choose your account")


def _password():
    return PasswordStep("visit-one", "accounts.example.test", PAGE, session="session-one")


def _state(step=None, **kwargs):
    return lifecycle.StepState(step, session="session-one", **kwargs)


def _event(name="probe", visible=False, **kwargs):
    return lifecycle.Event(name, _probe(visible=visible), session="session-one", **kwargs)


@pytest.mark.parametrize("state,event,expected", [
    (_state(_person()), lifecycle.Event("session_ended", session="session-one"),
     {"rules": ("R1",), "end": "unsettled", "clear_answers": True}),
    (_state(_person()), lifecycle.Event("probe", _probe("visit-two"), session="session-two"),
     {"rules": ("R2",), "end": "unsettled", "clear_answers": True}),
    (_state(_person(), row_state="accepted"), _event(),
     {"rules": ("R3",), "end": "done", "answer": lifecycle.Answer(1)}),
    (_state(_person(), row_state="closed"), _event(),
     {"rules": ("R4",), "end": "declined", "answer": lifecycle.Answer(1, True)}),
    (_state(_person()), lifecycle.Event("probe", _probe("visit-two"), session="session-one"),
     {"rules": ("R5",), "end": "settle", "answer": lifecycle.Answer(1)}),
    (_state(_password()), _event(), {"rules": ("R6",), "end": "settle", "answer": lifecycle.Answer(1)}),
    (_state(_person()), _event(), {"rules": ("R7",), "refusal": "person"}),
    (_state(answers={("password", "visit-one"): lifecycle.Answer(1, True)}), _event(visible=True),
     {"rules": ("R8",), "refusal": "password_declined"}),
    (_state(answers={("password", "visit-one"): lifecycle.Answer(1)}), _event(visible=True),
     {"rules": ("R9",), "open_kind": "password", "occurrence": 2, "refusal": "password"}),
    (_state(answers={("person", "visit-one"): lifecycle.Answer(1, True)}), _event("declare"),
     {"rules": ("R10",), "refusal": "person_declined"}),
    (_state(answers={("person", "visit-one"): lifecycle.Answer(1)}), _event("declare"),
     {"rules": ("R11",), "open_kind": "person", "occurrence": 2}),
    (_state(_person()), _event("declare", visible=True), {"rules": ("R12",)}),
    (_state(_password()), _event("declare"), {"rules": ("R13", "R7"), "refusal": "password"}),
], ids=[f"R{i}" for i in range(1, 14)])
def test_decide_rule_table_is_pure(state, event, expected):
    before = repr(state)
    decision = lifecycle.decide(state, event)
    for name, value in expected.items():
        assert getattr(decision, name) == value
    assert repr(state) == before
    assert lifecycle.decide(state, event) == decision


@pytest.mark.parametrize("kind", ["person", "password"])
@pytest.mark.parametrize("ending", ["terminate_lease", "shutdown", "session_change", "pane_session_change", "pane_session_gone"])
@pytest.mark.asyncio
async def test_session_end_never_settles_and_agent_close_is_refused(plan, monkeypatch, kind, ending):
    page, db = Page(), plan["db"]
    if kind == "person":
        await browser_control.declare_person_step(THREAD, page.agent_probe, "Choose your account", session="session-one")
    else:
        page.show("visit-one", frozenset({42}))
        with pytest.raises(BrowserSessionError, match="asks for a password"):
            await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",), session="session-one")
    [original] = _steps(db)
    owner = BrowserSessionOwner(namespace="corsia-step-test")
    runtime = BrowserRuntime(BrowserSessionRecord(THREAD, "session-one", lease_id="lease-one"))
    owner._sessions[THREAD] = runtime

    async def endpoint(_command):
        return PAGE

    async def probe(_endpoint, known):
        return await page.agent_probe(known)

    async def observe(_runtime):
        return SimpleNamespace(alive=False, artifacts_absent=True)

    monkeypatch.setattr(browser_cdp, "page_endpoint", endpoint)
    monkeypatch.setattr(browser_cdp, "page_password_probe", probe)
    monkeypatch.setattr(owner, "_observe", observe)
    with pytest.raises(BrowserSessionError, match="person's to complete"):
        await owner.close_thread(THREAD)
    assert owner._sessions[THREAD] is runtime
    assert row(db, original["id"])["state"] == "pending"
    if ending.startswith("pane_session"):
        browser_control.pane_connected(THREAD, "pane", None, page.pane_probe,
                                       session=lambda: owner.session_identity(THREAD))
        step = lifecycle._steps[THREAD]
        if ending == "pane_session_change":
            owner._sessions[THREAD] = BrowserRuntime(replace(runtime.record, session="session-two"))
        else:
            owner._sessions.pop(THREAD)
        page.show("visit-two")
        assert not await lifecycle._reprobe(THREAD, step)
    elif ending == "session_change":
        page.show("visit-two")
        await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",), session="session-two")
    else:
        if ending == "terminate_lease":
            await owner.terminate_lease("lease-one")
        else:
            await owner.shutdown()
        assert THREAD not in owner._sessions
        page.show("visit-two")
        await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",), session="session-two")
    assert row(db, original["id"])["state"] == "pending"
    assert THREAD not in lifecycle._steps
    assert not any(event.endswith("_settled") for event in plan["events"])
    assert not browser_control.password_page(THREAD)


@pytest.mark.parametrize("kind", ["person", "password"])
@pytest.mark.parametrize("operation", ["mark_done", "close_node"])
@pytest.mark.parametrize("hide_then_show", [False, True], ids=["field-stays", "field-reappears"])
@pytest.mark.asyncio
async def test_answers_end_live_steps_and_only_done_allows_a_new_occurrence(plan, kind, operation, hide_then_show):
    """Defects 2/3: both kinds honor a decline; a done row never remains the agent's wait target."""
    page, db = Page(), plan["db"]

    async def ask():
        if kind == "person":
            await browser_control.declare_person_step(THREAD, page.agent_probe, "Approve the sign-in")
        else:
            await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))

    if kind == "password":
        page.show("visit-one", frozenset({42}))
        with pytest.raises(BrowserSessionError, match="asks for a password"):
            await ask()
    else:
        await ask()
    [original] = _steps(db)
    params = close_params(db, operation, original["id"])
    if operation == "close_node":
        params["reason"] = "Not now"
    plan_update(db, plan["plan"].id, operation, params, actor=HUMAN)
    if hide_then_show:
        page.show("visit-one")
        await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    if kind == "password":
        page.show("visit-one", frozenset({42}))
    if operation == "close_node":
        for _ in range(2):
            with pytest.raises(BrowserSessionError, match="The person declined") as refused:
                await ask()
            assert "attention list" not in str(refused.value)
        assert len(_steps(db)) == 1
        assert THREAD not in lifecycle._steps
        assert plan["events"].count(f"{kind}_step_declined") == 1
    else:
        if kind == "password":
            with pytest.raises(BrowserSessionError, match="asks for a password"):
                await ask()
        else:
            await ask()
        first, second = _steps(db)
        assert first["id"] != second["id"] and second["state"] == "pending"
        assert lifecycle._steps[THREAD].node_id == second["id"]
        assert lifecycle._steps[THREAD].occurrence == 2
        assert not any(event.endswith("_declined") for event in plan["events"])


def test_other_target_never_settles_and_declare_cannot_end_a_password_step():
    step = _password()
    state = _state(step)
    other = lifecycle.Event("probe", _probe("visit-two", target="another-target"), session="session-one")
    assert lifecycle.decide(state, other).end is None
    assert lifecycle.decide(state, _event("declare")).end is None
    assert lifecycle.decide(state, _event(visible=True)).refusal == "password"


@pytest.mark.parametrize("kind", ["person", "password"])
@pytest.mark.asyncio
async def test_row_read_failure_preserves_the_live_step_and_retry_recovers(plan, monkeypatch, kind):
    from plans.public import PlanError

    page = Page()
    if kind == "person":
        await browser_control.declare_person_step(THREAD, page.agent_probe, "Choose your account")
    else:
        page.show("visit-one", frozenset({42}))
        with pytest.raises(BrowserSessionError):
            await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    step = lifecycle._steps[THREAD]
    original = lifecycle._step_row_state

    def fail(_node_id):
        raise PlanError("fixture row read failure")

    monkeypatch.setattr(lifecycle, "_step_row_state", fail)
    with pytest.raises(PlanError, match="fixture row read failure"):
        await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    assert lifecycle._steps[THREAD] is step
    assert row(plan["db"], step.node_id)["state"] == "pending"
    assert plan["events"] == [f"{kind}_step_opened"]
    monkeypatch.setattr(lifecycle, "_step_row_state", original)
    with pytest.raises(BrowserSessionError, match="person's to complete"):
        await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    assert lifecycle._steps[THREAD] is step
