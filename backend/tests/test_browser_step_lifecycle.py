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
     {"rules": ("R3",), "end": "done", "answer": lifecycle.Answer(1), "forget_done": "visit-one"}),
    (_state(_person(), row_state="closed"), _event(),
     {"rules": ("R4",), "end": "declined", "answer": lifecycle.Answer(1, True)}),
    (_state(_person()), lifecycle.Event("probe", _probe("visit-two"), session="session-one"),
     {"rules": ("R5",), "end": "settle", "answer": lifecycle.Answer(1)}),
    (_state(_password()), _event(), {"rules": ("R6",), "end": "settle", "answer": lifecycle.Answer(1)}),
    (_state(_person()), _event(), {"rules": ("R7",), "refusal": "person", "forget_done": "visit-one"}),
    (_state(answers={("password", "visit-one"): lifecycle.Answer(1, True)}), _event(visible=True),
     {"rules": ("R8",), "refusal": "password_declined"}),
    (_state(answers={("password", "visit-one"): lifecycle.Answer(1, nodes=frozenset({7}))}), _event(visible=True),
     {"rules": ("R9",), "open_kind": "password", "occurrence": 2, "refusal": "password", "forget_done": None}),
    (_state(answers={("person", "visit-one"): lifecycle.Answer(1, True)}), _event("declare"),
     {"rules": ("R10",), "refusal": "person_declined"}),
    (_state(answers={("person", "visit-one"): lifecycle.Answer(1)}), _event("declare"),
     {"rules": ("R11",), "open_kind": "person", "occurrence": 2}),
    (_state(_person()), _event("declare", visible=True), {"rules": ("R12",)}),
    (_state(_password()), _event("declare"), {"rules": ("R13", "R7"), "refusal": "password"}),
    (_state(answers={("password", "visit-one"): lifecycle.Answer(1, nodes=frozenset({42}))}), _event(visible=True),
     {"rules": ("R14",), "open_kind": None, "refusal": None, "forget_done": None}),
    (_state(answers={("password", "visit-one"): lifecycle.Answer(1, nodes=frozenset({42}))}), _event("declare", visible=True),
     {"rules": ("R11",), "open_kind": "person", "occurrence": 1, "refusal": None}),
    (_state(answers={("password", "visit-one"): lifecycle.Answer(1, nodes=frozenset({42})),
                     ("person", "visit-one"): lifecycle.Answer(1, True)}), _event("declare", visible=True),
     {"rules": ("R10",), "open_kind": None, "refusal": "person_declined"}),
    (_state(answers={("password", "visit-one"): lifecycle.Answer(1, nodes=frozenset({42, 43}))}),
     lifecycle.Event("probe", None, session="session-one"),
     {"rules": (), "forget_done": None}),
    (_state(_password(), row_state="accepted"), _event(),
     {"rules": ("R3",), "end": "done", "forget_done": "visit-one"}),
], ids=[f"R{i}" for i in range(1, 15)] + ["R14-declare-opens-person", "R14-declare-declined", "no-result-keeps-done", "forget-on-done-path"])
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
    # Never done: the row closes unfinished, with why, so the agent waiting on it hears it.
    assert row(db, original["id"])["state"] == "closed"
    reason = db.execute(
        "SELECT payload_json FROM plan_node_events WHERE node_id=? AND new_state='closed'", (original["id"],),
    ).fetchone()["payload_json"]
    assert lifecycle.SESSION_ENDED_REASON in reason
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
    elif kind == "password" and not hide_then_show:
        # R14: the person marked the field they were asked about done, so it no longer holds the agent.
        await ask()
        assert len(_steps(db)) == 1
        assert THREAD not in lifecycle._steps
        assert not browser_control.password_page(THREAD)
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


async def _done_with_field_showing(plan, page, nodes=frozenset({42})):
    """Open a sign-in step on a visible field, then have the person mark it done with the field still showing."""
    page.show("visit-one", nodes)
    with pytest.raises(BrowserSessionError, match="asks for a password"):
        await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    [original] = _steps(plan["db"])
    plan_update(plan["db"], plan["plan"].id, "mark_done", close_params(plan["db"], "mark_done", original["id"]),
                actor=HUMAN)


@pytest.mark.asyncio
async def test_a_done_answer_releases_snapshot_and_click_while_the_field_still_shows(plan):
    page = Page()
    await _done_with_field_showing(plan, page)
    for command in (("snapshot",), ("tab", "list"), ("click", "@e3")):
        assert await browser_control.require_page_access(THREAD, page.agent_probe, command) is None
    assert len(_steps(plan["db"])) == 1 and THREAD not in lifecycle._steps


@pytest.mark.asyncio
async def test_while_a_done_field_still_shows_only_commands_that_cannot_show_it_run(plan):
    """A field may display what the person typed (text type, show-password toggle), so reads that render it stay
    refused and a snapshot hides field values; once the field is gone everything runs again."""
    page = Page()
    await _done_with_field_showing(plan, page)
    for command in (("snapshot",), ("snapshot", "-i")):
        access = await browser_control.check_page_access(THREAD, page.agent_probe, command)
        assert access.filled_field_showing and access.note is None
    for command in (("screenshot", "shot.png"), ("pdf", "page.pdf"), ("get", "html", "body"), ("read",),
                    ("snapshot", "--json"), ("keyboard", "type", "x"), ("diff", "snapshot"), ("find", "role", "textbox")):
        with pytest.raises(BrowserSessionError, match="still shows the password field") as refused:
            await browser_control.check_page_access(THREAD, page.agent_probe, command)
        assert "attention list" not in str(refused.value)
    page.show("visit-one")
    for command in (("snapshot",), ("screenshot", "shot.png")):
        assert not (await browser_control.check_page_access(THREAD, page.agent_probe, command)).filled_field_showing
    assert len(_steps(plan["db"])) == 1


@pytest.mark.parametrize("snapshot, expected", [
    # Real agent-browser 0.31.2 lines: a text-type field and a toggled field show the value, a password field bullets.
    ("- textbox [ref=e1]: plain one\n- textbox [ref=e2]: Typed1", "- textbox [ref=e1]: [value hidden]\n- textbox [ref=e2]: [value hidden]"),
    ("- textbox [ref=e2]: Typed1\n  - StaticText \"Typed1\"\n- button \"Show\" [ref=e3]",
     "- textbox [ref=e2]: [value hidden]\n- button \"Show\" [ref=e3]"),
    ("- Iframe [ref=e2]\n  - textbox [ref=e3]: Typed1\n    - StaticText \"Typed1\"\n  - button \"Go\" [ref=e4]",
     "- Iframe [ref=e2]\n  - textbox [ref=e3]: [value hidden]\n  - button \"Go\" [ref=e4]"),
    ("- textbox \"Email\" [ref=e1] [required]: me]: x [ref=e9]", "- textbox \"Email\" [ref=e1] [required]: [value hidden]"),
    ("- textbox \"Email\" [ref=e1]\n- searchbox [ref=e2]", "- textbox \"Email\" [ref=e1]\n- searchbox [ref=e2]"),
    ("- combobox: Typed1", "- combobox [value hidden]"),
    ("- heading \"Sign in\" [level=1]\n- StaticText \"Welcome\"", "- heading \"Sign in\" [level=1]\n- StaticText \"Welcome\""),
])
def test_hide_field_values_hides_every_value_and_the_text_beneath_it(snapshot, expected):
    assert browser_control.hide_field_values(snapshot) == expected


@pytest.mark.asyncio
async def test_a_new_field_after_done_asks_the_person_again(plan):
    page = Page()
    await _done_with_field_showing(plan, page)
    page.show("visit-one", frozenset({42, 43}))
    with pytest.raises(BrowserSessionError, match="asks for a password"):
        await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    first, second = _steps(plan["db"])
    assert second["state"] == "pending" and lifecycle._steps[THREAD].occurrence == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("while_open", [frozenset({42, 43}), frozenset({44})], ids=["confirm_field", "replaced_input"])
async def test_a_field_shown_while_the_step_is_open_is_finished_with_it(plan, while_open):
    page = Page()
    page.show("visit-one", frozenset({42}))
    with pytest.raises(BrowserSessionError, match="asks for a password"):
        await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    # A confirm field appears, or the page swaps its input, while the person still has the step.
    page.show("visit-one", while_open)
    with pytest.raises(BrowserSessionError, match="asks for a password"):
        await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    [original] = _steps(plan["db"])
    plan_update(plan["db"], plan["plan"].id, "mark_done", close_params(plan["db"], "mark_done", original["id"]),
                actor=HUMAN)
    assert await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",)) is None
    assert len(_steps(plan["db"])) == 1 and THREAD not in lifecycle._steps


@pytest.mark.asyncio
async def test_a_field_hidden_then_shown_again_after_done_asks_the_person_again(plan):
    page = Page()
    await _done_with_field_showing(plan, page)
    assert await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",)) is None
    page.show("visit-one")
    assert await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",)) is None
    page.show("visit-one", frozenset({42}))
    with pytest.raises(BrowserSessionError, match="asks for a password"):
        await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    assert lifecycle._steps[THREAD].occurrence == 2


@pytest.mark.asyncio
async def test_declare_after_done_with_the_same_fields_showing_opens_a_person_step(plan):
    """The password part is finished; an e-signature or MFA step the agent declares still reaches the person."""
    page = Page()
    await _done_with_field_showing(plan, page)
    await browser_control.declare_person_step(THREAD, page.agent_probe, "Draw your signature")
    first, second = _steps(plan["db"])
    assert first["state"] != "pending" and second["state"] == "pending"
    assert isinstance(lifecycle._steps[THREAD], PersonStep)
    assert lifecycle._steps[THREAD].node_id == second["id"]
    with pytest.raises(BrowserSessionError, match="person"):
        await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))


@pytest.mark.asyncio
async def test_declare_after_done_when_a_new_field_shows_asks_the_password_again(plan):
    page = Page()
    await _done_with_field_showing(plan, page)
    page.show("visit-one", frozenset({42, 43}))
    with pytest.raises(BrowserSessionError, match="asks for a password"):
        await browser_control.declare_person_step(THREAD, page.agent_probe, "Draw your signature")
    first, second = _steps(plan["db"])
    assert second["state"] == "pending" and isinstance(lifecycle._steps[THREAD], PasswordStep)
    assert lifecycle._steps[THREAD].occurrence == 2


@pytest.mark.asyncio
async def test_declare_after_done_and_a_declined_person_step_stays_declined(plan):
    page = Page()
    await _done_with_field_showing(plan, page)
    await browser_control.declare_person_step(THREAD, page.agent_probe, "Draw your signature")
    _, person = _steps(plan["db"])
    plan_update(plan["db"], plan["plan"].id, "close_node",
                {**close_params(plan["db"], "close_node", person["id"]), "reason": "Not now"}, actor=HUMAN)
    with pytest.raises(BrowserSessionError, match="declined"):
        await browser_control.declare_person_step(THREAD, page.agent_probe, "Draw your signature")
    assert len(_steps(plan["db"])) == 2 and THREAD not in lifecycle._steps


@pytest.mark.asyncio
async def test_a_done_field_stays_read_guarded(plan):
    """The agent runs on the page again, but scripts and value reads of the field the person filled in stay refused."""
    page = Page()
    await _done_with_field_showing(plan, page)

    async def locate(node):
        async def resolve(_target):
            return node
        return resolve

    assert await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",)) is None
    with pytest.raises(BrowserSessionError, match="stay refused"):
        await browser_control.require_page_access(THREAD, page.agent_probe, ("eval", "document.title"))
    with pytest.raises(BrowserSessionError, match="stay refused"):
        await browser_control.require_page_access(THREAD, page.agent_probe, ("get", "value", "@e1"),
                                                  resolve=await locate(42))
    with pytest.raises(BrowserSessionError, match="stay refused"):
        await browser_control.require_page_access(THREAD, page.agent_probe, ("get", "value", "@e1"),
                                                  resolve=await locate(None))
    # A field that is not the done one reads as before.
    assert await browser_control.require_page_access(THREAD, page.agent_probe, ("get", "value", "@e2"),
                                                     resolve=await locate(7)) is None


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
    # A held step means its task exists, so its normal refusal holds even though the row can't be read.
    with pytest.raises(BrowserSessionError, match="asks for a password" if kind == "password" else "person's to complete"):
        await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    assert lifecycle._steps[THREAD] is step
    assert row(plan["db"], step.node_id)["state"] == "pending"
    assert plan["events"] == [f"{kind}_step_opened"]
    monkeypatch.setattr(lifecycle, "_step_row_state", original)
    with pytest.raises(BrowserSessionError, match="person's to complete"):
        await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    assert lifecycle._steps[THREAD] is step
