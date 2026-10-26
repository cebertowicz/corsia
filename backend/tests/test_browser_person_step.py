"""A page the agent hands to the person in the shared browser opens a needs-you step and wakes the agent.

These run the real guard, re-probe and plan writes against a test database; only the page, product
events and broadcasts are stand-ins.
"""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from host_access import browser_control, browser_steps
from host_access.browser_cdp import PasswordProbe
from host_access.browser_session_state import BrowserSessionError
from infra import state
from infra.database import get_db
from infra.db_fts import _init_messages_fts
from plans import node_states
from tests.plan_close_next_helpers import HUMAN, close_params, owner_plan, plan_update, row, select, task
from tests.test_plan_reconcile_release import seed_threads

PAGE = "ws://page.test/devtools/page/one"
OTHER = "ws://page.test/devtools/page/other"
THREAD = "owner"


class Page:
    """The one shared page that both the agent and the pane probe; a test moves it between visits."""

    def __init__(self):
        self.now = PasswordProbe("visit-one", "accounts.example.test", frozenset(), PAGE, present_nodes=frozenset())
        self.pane_probes = 0
        self.probed = asyncio.Event()

    def show(self, visit: str, nodes: frozenset[int] = frozenset(), target: str = PAGE,
             *, present: frozenset[int] | None = None) -> None:
        self.now = PasswordProbe(visit, "accounts.example.test", nodes, target,
                                 present_nodes=nodes if present is None else present)

    async def agent_probe(self, _visit, _known):
        return self.now

    async def pane_probe(self, _visit, _known):
        self.pane_probes += 1
        self.probed.set()
        return self.now

    async def reprobed(self, count: int = 3) -> None:
        target = self.pane_probes + count
        while self.pane_probes < target:
            self.probed.clear()
            await asyncio.wait_for(self.probed.wait(), 1)


@pytest_asyncio.fixture
async def plan(isolated_db, monkeypatch):
    """The agent's plan with its selected task, plus the product events and pane notices the page caused."""
    db = get_db()
    seed_threads(db)
    _init_messages_fts(db)
    db.commit()
    owner = owner_plan(db)
    mine = task(db, owner.id, "Read the page")
    select(db, owner.id, mine.id)
    recorded = {"db": db, "plan": owner, "task": mine, "events": [], "notices": []}
    monkeypatch.setattr(browser_control, "emit_product_event",
                        lambda _surface, action, **_kwargs: recorded["events"].append(action))
    monkeypatch.setattr(state, "broadcast_to_thread",
                        lambda _thread, message: recorded["notices"].append(message["visible"]))
    monkeypatch.setattr(browser_control, "REPROBE_EVERY", 0.01)
    yield recorded
    step = browser_control._steps.get(THREAD)
    reprobe = step.reprobe if step is not None else None
    browser_control.pane_disconnected(THREAD, "pane")
    if reprobe is not None:
        await asyncio.gather(reprobe, return_exceptions=True)
    for registry in (browser_control._steps, browser_control._guarded_visits, browser_control._probe_locks):
        registry.clear()


def _steps(db) -> list:
    return db.execute("SELECT id, title, state FROM plan_nodes WHERE assignee='user' ORDER BY created_at").fetchall()


async def _until(predicate) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never held")


@pytest.mark.asyncio
async def test_a_person_step_stays_open_on_its_visit_settles_on_a_new_one_and_wakes_the_task(plan):
    """An account chooser has no password field; the step waits for the person, then wakes the agent."""
    page, db = Page(), plan["db"]
    browser_control.pane_connected(THREAD, "pane", None, page.pane_probe)
    await browser_control.declare_person_step(THREAD, page.agent_probe, "Choose your account")

    await page.reprobed()
    [step] = _steps(db)
    assert (step["title"], step["state"]) == ("Complete this step in the browser (example.test)", "pending")
    assert not browser_control.password_page(THREAD)

    # A password field on the step's own visit (an email-first page's second screen) keeps it the person's.
    page.show("visit-one", frozenset({42}))
    await page.reprobed()
    assert row(db, step["id"])["state"] == "pending"

    # Wait for the whole re-probe, including the event and in-memory cleanup after the plan write.
    reprobe = browser_control._steps[THREAD].reprobe
    page.show("visit-two")
    await asyncio.wait_for(reprobe, 1)
    assert row(db, step["id"])["state"] == "accepted"
    mine = plan["task"]
    wake = db.execute("SELECT content FROM messages WHERE id=?",
                      (f"plan-wake:work_selected:{mine.id}:{row(db, mine.id)['revision']}",)).fetchone()
    assert "Read the page is now your current task" in wake["content"]
    assert plan["events"] == ["person_step_opened", "person_step_settled"]
    # The agent's next command on the new page runs.
    await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["password", "person"])
async def test_a_reprobe_on_another_target_settles_neither_kind(plan, kind):
    """Only the agent's own probe, or one of the step's target, can end a step; a popup comes back."""
    page, db = Page(), plan["db"]
    browser_control.pane_connected(THREAD, "pane", None, page.pane_probe)
    if kind == "password":
        page.show("visit-one", frozenset({42}))
        with pytest.raises(BrowserSessionError, match="asks for a password"):
            await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    else:
        await browser_control.declare_person_step(THREAD, page.agent_probe, "Sign in to your account")
    [step] = _steps(db)

    page.show("visit-popup", target=OTHER)
    await page.reprobed()
    assert row(db, step["id"])["state"] == "pending"
    assert browser_control.password_page(THREAD) is (kind == "password")

    # The person finishes in the popup and the step's own tab navigates.
    # Wait for the whole re-probe, including the event and in-memory cleanup after the plan write.
    reprobe = browser_control._steps[THREAD].reprobe
    page.show("visit-two")
    await asyncio.wait_for(reprobe, 1)
    assert row(db, step["id"])["state"] == "accepted"
    assert not browser_control.password_page(THREAD)


@pytest.mark.asyncio
async def test_a_declare_on_an_open_password_step_leaves_it_a_password_step(plan):
    """The declaration is refused like any command on a password page, and never rewrites the step."""
    page, db = Page(), plan["db"]
    page.show("visit-one", frozenset({42}))
    with pytest.raises(BrowserSessionError, match="asks for a password") as agent:
        await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    with pytest.raises(BrowserSessionError) as declared:
        await browser_control.declare_person_step(THREAD, page.agent_probe, "Choose your account")
    # The same guard gives the same refusal and the same ending.
    assert str(declared.value) == str(agent.value)

    # The field hides without navigating; the open password step still refuses the declaration.
    page.show("visit-one")
    with pytest.raises(BrowserSessionError, match="asks for a password"):
        await browser_control.declare_person_step(THREAD, page.agent_probe, "Choose your account")
    [step] = _steps(db)
    assert step["title"] == "Enter your password in the browser (example.test)"
    assert browser_control.password_page(THREAD)

    page.show("visit-two")
    await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    assert row(db, step["id"])["state"] == "accepted"
    assert not browser_control.password_page(THREAD)
    assert plan["notices"] == [True, True, False]


@pytest.mark.asyncio
@pytest.mark.parametrize("pane", [True, False], ids=["pane", "no-pane"])
async def test_after_the_person_declines_a_person_step_the_agents_next_command_runs(plan, pane):
    """AR7: the decline is counted once, and the agent is told how to go on instead of being refused."""
    page, db = Page(), plan["db"]
    if pane:
        browser_control.pane_connected(THREAD, "pane", None, page.pane_probe)
    await browser_control.declare_person_step(THREAD, page.agent_probe, "Approve the sign-in on your phone")
    with pytest.raises(BrowserSessionError, match="person's to complete") as refused:
        await browser_control.require_page_access(THREAD, page.agent_probe, ("click", "@e1"))
    assert "If they decline" in str(refused.value)

    [step] = _steps(db)
    plan_update(db, plan["plan"].id, "close_node",
                {**close_params(db, "close_node", step["id"]), "reason": "Not now"}, actor=HUMAN)
    if pane:
        await _until(lambda: "person_step_declined" in plan["events"])
    for _ in range(2):
        await browser_control.require_page_access(THREAD, page.agent_probe, ("click", "@e1"))
    assert plan["events"].count("person_step_declined") == 1


@pytest.mark.asyncio
async def test_a_person_marking_the_step_done_is_not_a_decline(plan):
    page, db = Page(), plan["db"]
    await browser_control.declare_person_step(THREAD, page.agent_probe, "Approve the sign-in on your phone")
    [step] = _steps(db)
    plan_update(db, plan["plan"].id, "mark_done", close_params(db, "mark_done", step["id"]), actor=HUMAN)
    await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    assert "person_step_declined" not in plan["events"]


@pytest.mark.asyncio
async def test_a_second_declare_on_the_same_visit_keeps_the_one_open_step(plan):
    page, db = Page(), plan["db"]
    await browser_control.declare_person_step(THREAD, page.agent_probe, "Choose your account")
    await browser_control.declare_person_step(THREAD, page.agent_probe, "Choose your account")
    assert len(_steps(db)) == 1 and plan["events"] == ["person_step_opened"]


@pytest.mark.asyncio
async def test_a_password_field_seen_during_a_person_step_keeps_the_visit_read_guarded(plan):
    """AR2: once the person's step ends on the same document, its hidden password input stays unreadable."""
    page, db = Page(), plan["db"]
    browser_control.pane_connected(THREAD, "pane", None, page.pane_probe)
    await browser_control.declare_person_step(THREAD, page.agent_probe, "Sign in to your account")
    page.show("visit-one", frozenset({42}))
    await page.reprobed()
    page.show("visit-one")
    [step] = _steps(db)
    plan_update(db, plan["plan"].id, "mark_done", close_params(db, "mark_done", step["id"]), actor=HUMAN)
    for command in (("eval", "document.body.innerText"), ("get", "value", "#password")):
        with pytest.raises(BrowserSessionError, match="asks for a password"):
            await browser_control.require_page_access(THREAD, page.agent_probe, command)
    await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["close_node", "mark_done"], ids=["declined", "marked-done"])
@pytest.mark.parametrize("pane", [True, False], ids=["pane", "no-pane"])
@pytest.mark.parametrize("observed", [True, False], ids=["already-observed", "not-yet-observed"])
async def test_redeclaring_an_ended_person_step_refuses_without_reopening_or_counting_it_again(
    plan, monkeypatch, operation, pane, observed,
):
    """A reused ended receipt cannot promise a new handoff or wait for a wake that will never arrive."""
    page, db = Page(), plan["db"]
    if pane:
        browser_control.pane_connected(THREAD, "pane", None, page.pane_probe)
    await browser_control.declare_person_step(THREAD, page.agent_probe, "Choose your account")
    original = browser_control._steps[THREAD]
    loop = original.reprobe
    [step] = _steps(db)
    params = close_params(db, operation, step["id"])
    plan_update(db, plan["plan"].id, operation,
                {**params, "reason": "Not now"} if operation == "close_node" else params, actor=HUMAN)
    if observed:
        await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))

    starts = []
    start_reprobe = browser_control._start_reprobe

    def record_start(*args):
        starts.append(args)
        start_reprobe(*args)

    monkeypatch.setattr(browser_control, "_start_reprobe", record_start)
    for _ in range(2):
        with pytest.raises(BrowserSessionError) as refused:
            await browser_control.declare_person_step(THREAD, page.agent_probe, "Choose your account")
        assert str(refused.value) == (
            "The person already ended the step for this page, so it can't be given to them again. "
            "Tell them in chat what you needed the page for and stop."
        )
        assert THREAD not in browser_control._steps
        await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    if loop is not None:
        await asyncio.gather(loop, return_exceptions=True)
    assert starts == [] and original.reprobe is None
    assert len(_steps(db)) == 1
    assert plan["events"] == ["person_step_opened"] + (
        ["person_step_declined"] if operation == "close_node" else []
    )


@pytest.mark.asyncio
async def test_an_ended_person_step_then_a_password_on_the_same_visit_opens_its_own_row(plan):
    page, db = Page(), plan["db"]
    await browser_control.declare_person_step(THREAD, page.agent_probe, "Choose your account")
    [person] = _steps(db)
    plan_update(db, plan["plan"].id, "mark_done", close_params(db, "mark_done", person["id"]), actor=HUMAN)
    page.show("visit-one", frozenset({42}))
    for _ in range(2):
        with pytest.raises(BrowserSessionError, match="asks for a password"):
            await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    person, password = _steps(db)
    assert person["id"] != password["id"] and password["state"] == "pending"
    assert db.execute("SELECT 1 FROM plan_node_dependencies WHERE node_id=? AND depends_on_node_id=?",
                      (plan["task"].id, password["id"])).fetchone()
    assert isinstance(browser_control._steps[THREAD], browser_steps.PasswordStep)
    assert plan["notices"][-1] is True
    assert plan["events"].count("password_step_opened") == 1


@pytest.mark.asyncio
async def test_a_settled_password_then_a_person_step_on_the_same_visit_opens_its_own_row(plan):
    page, db = Page(), plan["db"]
    page.show("visit-one", frozenset({42}))
    with pytest.raises(BrowserSessionError, match="asks for a password"):
        await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    page.show("visit-one")
    await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    await browser_control.declare_person_step(THREAD, page.agent_probe, "Approve on your phone")
    await browser_control.declare_person_step(THREAD, page.agent_probe, "Approve on your phone")
    password, person = _steps(db)
    assert password["id"] != person["id"] and password["state"] == "accepted" and person["state"] == "pending"
    assert db.execute("SELECT 1 FROM plan_node_dependencies WHERE node_id=? AND depends_on_node_id=?",
                      (plan["task"].id, person["id"])).fetchone()
    assert isinstance(browser_control._steps[THREAD], browser_steps.PersonStep)
    assert plan["events"].count("person_step_opened") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("ending_probe", ["agent", "pane", "pane-navigation"])
@pytest.mark.parametrize("command", [("eval", "document.querySelector('#password').value"),
                                    ("get", "value", "#password")], ids=["eval", "get-value"])
async def test_a_password_hidden_between_probes_is_guarded_before_the_person_step_is_dropped(
    plan, monkeypatch, ending_probe, command,
):
    """The end-time probe finds the hidden input even when no probe observed it while visible."""
    page, db = Page(), plan["db"]
    monkeypatch.setattr(browser_control, "REPROBE_EVERY", 60)
    browser_control.pane_connected(THREAD, "pane", None, page.pane_probe)
    await browser_control.declare_person_step(THREAD, page.agent_probe, "Sign in to your account")
    active = browser_control._steps[THREAD]
    loop = active.reprobe
    # No await between appearing and hiding: neither the pane nor agent sampled the visible field.
    page.show("visit-one", frozenset({42}))
    page.show("visit-one", present=frozenset({42}))
    assert THREAD not in browser_control._guarded_visits
    [person] = _steps(db)
    if ending_probe == "pane-navigation":
        page.show("visit-two", present=frozenset({42}))
    else:
        plan_update(db, plan["plan"].id, "mark_done", close_params(db, "mark_done", person["id"]), actor=HUMAN)
    if ending_probe.startswith("pane"):
        assert not await browser_control._reprobe(THREAD, active)

    async def resolve(_selector):
        return 42

    with pytest.raises(BrowserSessionError, match="asks for a password"):
        await browser_control.require_page_access(THREAD, page.agent_probe, command, resolve=resolve)
    await asyncio.gather(loop, return_exceptions=True)
    assert THREAD not in browser_control._steps
    assert browser_control._guarded_visits[THREAD].seen_nodes == frozenset({42})
    assert set(vars(page.now)) == {"visit", "host", "target", "visible_nodes", "present_nodes"}
    assert not any("value" in name or "text" in name for name in vars(browser_control._guarded_visits[THREAD]))
    assert plan["events"] == ["person_step_opened"] + (
        ["person_step_settled"] if ending_probe == "pane-navigation" else []
    )


@pytest.mark.parametrize("instruction", ["Choose your account.", "Your phone needs approval", "Sign in"])
def test_person_step_keeps_free_form_instruction_out_of_product_chrome(instruction):
    step = browser_steps.PersonStep("visit", "accounts.example.test", instruction=instruction)
    assert step.title() == "Complete this step in the browser (example.test)"
    assert step.description() == (
        "Complete this step in the shared browser. The agent can't see this page and carries on once you're past it."
        f"\n\n{instruction}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["person", "password"])
@pytest.mark.parametrize("stage", ["write", "read"])
async def test_failed_row_open_never_publishes_an_active_step_and_retry_can_open(plan, monkeypatch, kind, stage):
    from plans.public import PlanError

    page = Page()
    if kind == "password":
        page.show("visit-one", frozenset({42}))
    boundary = "_plan_write" if stage == "write" else "_step_row_state"
    original = getattr(browser_control, boundary)

    def fail(*_args, **_kwargs):
        raise PlanError("fixture row failure")

    monkeypatch.setattr(browser_control, boundary, fail)
    with pytest.raises(PlanError, match="fixture row failure"):
        if kind == "person":
            await browser_control.declare_person_step(THREAD, page.agent_probe, "Choose your account")
        else:
            await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    assert THREAD not in browser_control._steps
    existing = _steps(plan["db"])
    assert len(existing) == (0 if stage == "write" else 1)
    assert plan["events"] == []
    monkeypatch.setattr(browser_control, boundary, original)
    if kind == "person":
        await browser_control.declare_person_step(THREAD, page.agent_probe, "Choose your account")
    else:
        with pytest.raises(BrowserSessionError, match="asks for a password"):
            await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
    [row] = _steps(plan["db"])
    assert browser_control._steps[THREAD].node_id == row["id"]
    if existing:
        assert row["id"] == existing[0]["id"]
    assert plan["events"] == [f"{kind}_step_opened"]


def test_the_done_states_a_decline_is_told_from_are_the_plans_accepted_states():
    """A person's Mark done must never read as a decline, whatever states the plan accepts."""
    assert browser_steps.DONE_STATES == node_states._ACCEPTED_STATES
