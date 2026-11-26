"""A person step's end starts the waiting agent's Turn, not just queues its wake (#3191).

The step's end appends the agent's plan wakes (``test_plan_system_user_steps``).
These tests go one step further, on the database the driver reads: once the
write commits, the agent's idle FIFO head is claimed by a new Turn carrying
those wakes, with no other input. The person's Mark done reaches the driver
through the plans route (``boundary.mutate`` then ``drive_deferred``); the
system's own settle or close has no route around it, so the plan write itself
hands the drive to the application loop after it commits.
"""

from __future__ import annotations

import asyncio

import pytest

from infra.database import get_db
from infra.thread_input_types import ClaimedBatch
from plans import public
from plans.boundary import mutate
from tests.event_routing_helpers import mk_session
from tests.plan_close_next_helpers import HUMAN, close_params, owner_plan, select, task

PROVENANCE = "browser.person_step"
TITLE = "Your turn in the shared browser (example.com)"


@pytest.fixture
def started(isolated_db, monkeypatch):
    """Record each Turn the idle driver starts instead of running a model."""
    import infra.thread_execution as execution

    mk_session("owner")
    db = get_db()  # the plan helpers' OWNER actor belongs to user 'user'
    db.execute("UPDATE threads SET user_id='user' WHERE id='owner'")
    db.commit()
    db.close()
    claimed: list[ClaimedBatch] = []

    async def record(admission):
        claimed.append(admission)

    monkeypatch.setattr(execution, "drive_turn_admission", record)
    return claimed


@pytest.fixture
def submitted(monkeypatch):
    """The coroutines a committed write hands the application loop, in order."""
    import infra.app_loop as app_loop

    handed: list = []
    monkeypatch.setattr(app_loop, "submit", handed.append)
    return handed


def _waiting_step(db) -> tuple[str, str, str]:
    """The agent's selected task, waiting on a step the system opened for the person."""
    plan = owner_plan(db)
    mine = task(db, plan.id, "File the claim")
    select(db, plan.id, mine.id)
    step = public.open_user_step(
        db, "owner", key="visit-1", title=TITLE, description=TITLE, provenance=PROVENANCE,
    )
    db.commit()
    return plan.id, mine.id, step


def _wake_ids(batch: ClaimedBatch) -> set[str]:
    return {item.message_id for item in batch.items if item.message_id.startswith("plan-wake:")}


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["settle", "close"])
async def test_a_system_ended_step_starts_the_agents_turn(started, submitted, ending):
    """The page moved on (settle) or the session ended (close): the agent runs with nobody typing."""
    db = get_db()
    try:
        _plan_id, mine, step = _waiting_step(db)
        if ending == "settle":
            ended = await asyncio.to_thread(
                _off_loop, public.settle_user_step, step,
                evidence="The shared browser moved on to a new page.",
            )
        else:
            ended = await asyncio.to_thread(
                _off_loop, public.close_user_step, step,
                reason="The shared browser closed before this task was finished.",
            )
        assert ended is True
        queued = {r["id"] for r in db.execute(
            "SELECT id FROM messages WHERE thread_id='owner' AND id LIKE 'plan-wake:%' "
            "AND delivery_state='queued' AND active_turn_id IS NULL",
        ).fetchall()}
    finally:
        db.close()
    assert queued, "the step's end queued no wake for the agent"

    for coroutine in submitted:
        await coroutine

    assert [batch.thread_id for batch in started] == ["owner"], "no Turn started from the step's end"
    assert isinstance(started[0], ClaimedBatch)
    assert queued <= _wake_ids(started[0])


def test_a_step_ends_even_when_its_drive_cannot_be_scheduled(started, submitted, monkeypatch):
    """A transaction this write doesn't own can't take the drive: the step still ends, its wake stays queued."""
    import infra.write_effects as write_effects
    from infra.database import TransactionOwnershipError

    def not_owned(_connection, _effect):
        raise TransactionOwnershipError("not owned here")

    db = get_db()
    try:
        _plan_id, _mine, step = _waiting_step(db)
        monkeypatch.setattr(write_effects, "after_commit", not_owned)
        assert _off_loop(public.settle_user_step, step, evidence="The shared browser moved on to a new page.") is True
        state = db.execute("SELECT state FROM plan_nodes WHERE id=?", (step,)).fetchone()[0]
        queued = db.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id='owner' AND id LIKE 'plan-wake:%' "
            "AND delivery_state='queued'",
        ).fetchone()[0]
    finally:
        db.close()
    assert state == "accepted" and queued > 0
    assert started == []


def _off_loop(write, step: str, **word) -> bool:
    """The browser's own call shape: a dedicated connection on a worker thread."""
    db = get_db()
    try:
        return write(db, step, provenance=PROVENANCE, **word)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_the_persons_mark_done_starts_the_agents_turn(started, submitted):
    """The person's Done goes through the plans route, which drives the woken thread after commit."""
    db = get_db()
    try:
        plan_id, _mine, step = _waiting_step(db)
        result = mutate(
            plan_id, "mark_done", close_params(db, "mark_done", step),
            actor=HUMAN, provenance="api.plans", mutation_id="person-done", db=db,
        )
        db.commit()
    finally:
        db.close()

    public.deliver_deferred(result)
    await public.drive_deferred(result)
    for coroutine in submitted:  # the write's own plan nudges
        await coroutine

    assert [batch.thread_id for batch in started] == ["owner"]
    assert any(wake.startswith("plan-wake:") for wake in _wake_ids(started[0]))


def _running_turn(db, turn_id: str) -> None:
    """The agent's own Turn, still running when the step ends."""
    from core.turn_core import persist_turn_start
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.thread_driver import admit_input

    admission = admit_input(
        "owner", role="user", content="Fill the form", intent="apply_now",
        message_id=f"{turn_id}:input", turn_id=turn_id, boot_epoch="test-boot",
        admitted_at="t", db=db,
    )
    assert admission.batch is not None
    begin_batch_write(admission.batch, attempt_id=f"{turn_id}:write", started_at="t", db=db)
    settle_batch_write(f"{turn_id}:write", outcome="accepted", db=db)
    persist_turn_start(turn_id, "owner", "opus", "Fill the form", db=db)
    db.commit()


@pytest.mark.asyncio
async def test_a_step_ending_while_the_agents_turn_ends_is_claimed_by_its_successor(
    started, submitted, monkeypatch,
):
    """The step ends while the agent's Turn is still running: the drive finds it busy, and the
    Turn's own end claims the wakes as its successor's input rather than leaving them queued."""
    import infra.thread_execution as execution
    from infra.thread_driver import finish_turn

    boundaries: list[str] = []

    async def boundary(thread_id):
        boundaries.append(thread_id)

    monkeypatch.setattr(execution, "_request_active_boundary", boundary)
    db = get_db()
    try:
        _plan_id, _mine, step = _waiting_step(db)
        _running_turn(db, "turn-1")
    finally:
        db.close()
    assert await asyncio.to_thread(
        _off_loop, public.settle_user_step, step, evidence="The shared browser moved on to a new page.",
    ) is True
    for coroutine in submitted:
        await coroutine
    assert started == [] and boundaries == ["owner"]

    settlement = await asyncio.to_thread(
        finish_turn, "turn-1", outcome="completed", ended_at="t2",
        successor_turn_id="turn-2", boot_epoch="test-boot",
    )

    assert isinstance(settlement.successor, ClaimedBatch)
    assert any(wake.startswith("plan-wake:") for wake in _wake_ids(settlement.successor))
    for coroutine in submitted:  # the Turn end's own plan nudges; closing an awaited one is a no-op
        coroutine.close()
