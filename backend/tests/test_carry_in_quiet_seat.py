"""C1 carry-in on a shared seat: the drain waits for other clients to go quiet, then checks once."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.test_carry_in_lifecycle import (  # noqa: F401 - c1 is the shared fixture
    ACTIONS, _admitting_turn, _arm, _events, _make_pending, _pending_fact, _save, _write_lesson, c1,
)
from tests.test_carry_in_retest_delivery import _read_turn_result

MODEL = "omlx@srv/model"


def _seat(monkeypatch, *, quiet_at_claim: bool, quiet_at_sample: bool):
    """A seat busy with another client's request; each wait ends it only where the flag says."""
    from infra import carry_in_capacity

    seat = SimpleNamespace(busy=True, log=[])

    async def wait_quiet(bound=None):
        seat.log.append(("wait", bound))
        seat.busy = not (quiet_at_claim if bound is None else quiet_at_sample)

    async def ready_idle(bound=None, exclude_thread=None):
        seat.log.append(("check", bound))
        return None if seat.busy else MODEL

    monkeypatch.setattr(carry_in_capacity, "wait_quiet", wait_quiet)
    monkeypatch.setattr(carry_in_capacity, "ready_idle", ready_idle)
    return seat


async def _pending(c1):
    _write_lesson(c1)
    _save(c1, "rule v1\n")
    await _make_pending(c1)
    return _pending_fact()


@pytest.mark.asyncio
async def test_a_busy_seat_that_goes_quiet_lets_the_claim_run(c1, monkeypatch):
    from infra import carry_in_drain

    await _pending(c1)
    seat = _seat(monkeypatch, quiet_at_claim=True, quiet_at_sample=True)
    models = []

    async def sample(fact, owner, model, actions, arm, label, created):
        models.append(model)
        return label[0] == "c"

    monkeypatch.setattr(carry_in_drain, "_sample", sample)
    assert await carry_in_drain.wake(c1.workspace) is True
    assert seat.log == [("wait", None), ("check", None)]
    assert models and set(models) == {MODEL}
    assert (_events()[-1]["event"], _events()[-1]["verdict"]) == ("lesson.carry_in.result", "passed")


@pytest.mark.asyncio
async def test_a_seat_busy_throughout_the_claim_leaves_the_case_waiting_as_before(c1, monkeypatch):
    from infra import carry_in_drain

    await _pending(c1)
    seat = _seat(monkeypatch, quiet_at_claim=False, quiet_at_sample=False)
    monkeypatch.setattr(carry_in_drain, "_sample", lambda *a: pytest.fail("a sample ran"))

    assert await carry_in_drain.wake(c1.workspace) is False
    assert seat.log == [("wait", None), ("check", None)]
    assert (_events()[-1]["event"], _events()[-1]["reason"]) == ("lesson.carry_in.waiting", "no_ready_local_model")
    assert _events("lesson.carry_in.coverage") == []


@pytest.mark.asyncio
async def test_a_busy_seat_that_goes_quiet_before_a_sample_lets_it_run_and_count(c1, monkeypatch):
    from eval import endpoint, turn_result
    from infra import carry_in_drain

    fact = await _pending(c1)
    seat = _seat(monkeypatch, quiet_at_claim=True, quiet_at_sample=True)
    dispatched: list = []
    monkeypatch.setattr(endpoint, "run_eval_turn", _admitting_turn(dispatched))
    monkeypatch.setattr(turn_result, "read_turn_result", _read_turn_result("b"))
    completed = [0]

    passes = await carry_in_drain._run_side(
        fact, {"user_id": "steve", "project_id": None}, MODEL, ACTIONS,
        _arm(c1, fact, deliver=False), "b", [], completed,
    )
    assert (passes, completed[0]) == (2, 2) and len(dispatched) == 2
    # Each sample waits first, then the one-shot check inside its Turn runs.
    assert seat.log == [("wait", MODEL), ("check", MODEL)] * 2


@pytest.mark.asyncio
async def test_a_seat_busy_throughout_a_sample_ends_the_claim_as_before(c1, monkeypatch):
    from eval import endpoint, turn_result
    from infra import carry_in_drain

    await _pending(c1)
    seat = _seat(monkeypatch, quiet_at_claim=True, quiet_at_sample=False)
    dispatched: list = []
    monkeypatch.setattr(endpoint, "run_eval_turn", _admitting_turn(dispatched))
    monkeypatch.setattr(turn_result, "read_turn_result", lambda *_a: pytest.fail("read an undispatched turn"))

    assert await carry_in_drain.wake(c1.workspace) is True
    assert dispatched == []
    assert (_events()[-1]["event"], _events()[-1]["reason"]) == ("lesson.carry_in.coverage", "no_ready_local_model")
    assert seat.log[-2:] == [("wait", MODEL), ("check", MODEL)]


@pytest.mark.asyncio
async def test_wait_quiet_waits_on_the_bound_models_server_for_the_guard_bound(monkeypatch):
    from infra import carry_in_capacity
    from llm import provider_overflow

    waits = []

    async def row(_fn, exclude_thread):
        return (MODEL, "srv")

    async def until_quiet(server_id, *, timeout, interval):
        waits.append((server_id, timeout, interval))
        return False

    monkeypatch.setattr(carry_in_capacity, "run_off_db", row)
    monkeypatch.setattr(carry_in_capacity, "wait_until_quiet", until_quiet)

    await carry_in_capacity.wait_quiet("omlx@srv/other")  # never waits for a row it will not use
    assert waits == []
    await carry_in_capacity.wait_quiet(MODEL)
    await carry_in_capacity.wait_quiet()
    expected = ("srv", provider_overflow.GUARD_QUIET_TIMEOUT_SECONDS, provider_overflow.GUARD_QUIET_INTERVAL_SECONDS)
    assert waits == [expected, expected]
