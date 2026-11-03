"""C1 carry-in on a shared seat: a busy reading never ends a claimed case while the seat goes quiet.

Every test drives the real ``ready_idle`` -> ``wait_until_quiet`` -> ``omlx_state`` path; only the
status feed (a scripted sequence of readings) and the one local row are stubbed.
"""

from __future__ import annotations

import time

import pytest

from tests.test_carry_in_lifecycle import (  # noqa: F401 - c1 is the shared fixture
    ACTIONS, _admitting_turn, _arm, _events, _make_pending, _pending_fact, _save, _write_lesson, c1,
)
from tests.test_carry_in_retest_delivery import _read_turn_result

MODEL = "omlx@srv/model"
OWNER = {"user_id": "steve", "project_id": None}


def _status_seat(monkeypatch, readings, *, timeout=None):
    """The one local row, and a status feed that serves ``readings`` in order, then repeats the last."""
    from infra import carry_in_capacity
    from llm import provider_overflow

    seen = []

    async def row(_fn, exclude_thread):
        return (MODEL, "srv")

    async def probe(_client, _server_id, _host, _port):
        seen.append(readings[min(len(seen), len(readings) - 1)])
        return None if seen[-1] is None else ("srv", None, seen[-1])

    monkeypatch.setattr(carry_in_capacity, "run_off_db", row)
    monkeypatch.setattr(carry_in_capacity, "_status_address", lambda _server_id: ("127.0.0.1", 1))
    monkeypatch.setattr("llm.providers.omlx_public_probe.probe_registered_status", probe)
    monkeypatch.setattr(carry_in_capacity, "IDLE_GAP_SECONDS", 0)
    monkeypatch.setattr(provider_overflow, "GUARD_QUIET_INTERVAL_SECONDS", 0.01)
    if timeout is not None:
        monkeypatch.setattr(provider_overflow, "GUARD_QUIET_TIMEOUT_SECONDS", timeout)
    return seen


def _reading(active=0, waiting=0, total=5):
    return {"active_requests": active, "waiting_requests": waiting, "total_requests": total}


BUSY, QUIET = _reading(active=1), _reading()


async def _pending(c1):
    _write_lesson(c1)
    _save(c1, "rule v1\n")
    await _make_pending(c1)
    return _pending_fact()


@pytest.mark.asyncio
async def test_ready_idle_waits_through_busy_and_bursty_readings_with_one_proof_per_try(monkeypatch):
    from infra import carry_in_capacity

    # busy; quiet then a neighbour's new request (total moved); quiet, quiet: the third try passes.
    seen = _status_seat(monkeypatch, [BUSY, _reading(total=5), _reading(total=6), QUIET, QUIET])
    assert await carry_in_capacity.ready_idle(MODEL, wait=5.0) == MODEL
    assert len(seen) == 5


@pytest.mark.asyncio
async def test_ready_idle_without_a_wait_is_one_proof(monkeypatch):
    from infra import carry_in_capacity

    seen = _status_seat(monkeypatch, [BUSY])
    assert await carry_in_capacity.ready_idle(MODEL) is None
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_ready_idle_returns_at_the_bound_on_a_seat_that_stays_busy(monkeypatch):
    from infra import carry_in_capacity

    seen = _status_seat(monkeypatch, [BUSY])
    assert await carry_in_capacity.ready_idle(MODEL, wait=0.05) is None
    assert len(seen) >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize("readings", [[None], [{"status": "ok"}], [QUIET, {"active_requests": 0}]])
async def test_ready_idle_returns_at_once_when_the_status_is_unreadable(monkeypatch, readings):
    from infra import carry_in_capacity

    seen = _status_seat(monkeypatch, readings)
    started = time.monotonic()
    assert await carry_in_capacity.ready_idle(MODEL, wait=600.0) is None
    assert time.monotonic() - started < 5 and len(seen) <= 2


@pytest.mark.asyncio
async def test_ready_idle_never_waits_for_a_row_it_will_not_use(monkeypatch):
    from infra import carry_in_capacity

    seen = _status_seat(monkeypatch, [BUSY])
    assert await carry_in_capacity.ready_idle("omlx@srv/other", wait=600.0) is None
    assert seen == []


@pytest.mark.asyncio
async def test_a_busy_seat_that_goes_quiet_lets_the_claim_run(c1, monkeypatch):
    from infra import carry_in_drain

    await _pending(c1)
    seen = _status_seat(monkeypatch, [BUSY, QUIET])
    models = []

    async def sample(fact, owner, model, actions, arm, label, created):
        models.append(model)
        return label[0] == "c"

    monkeypatch.setattr(carry_in_drain, "_sample", sample)
    assert await carry_in_drain.wake(c1.workspace) is True
    assert len(seen) == 3  # busy, then one two-read proof; no second check after the wait
    assert models and set(models) == {MODEL}
    assert (_events()[-1]["event"], _events()[-1]["verdict"]) == ("lesson.carry_in.result", "passed")


@pytest.mark.asyncio
async def test_a_seat_busy_throughout_the_claim_leaves_the_case_waiting(c1, monkeypatch):
    from infra import carry_in_drain

    await _pending(c1)
    _status_seat(monkeypatch, [BUSY], timeout=0.05)
    monkeypatch.setattr(carry_in_drain, "_sample", lambda *a: pytest.fail("a sample ran"))

    assert await carry_in_drain.wake(c1.workspace) is False
    assert (_events()[-1]["event"], _events()[-1]["reason"]) == ("lesson.carry_in.waiting", "no_ready_local_model")
    assert _events("lesson.carry_in.coverage") == []


@pytest.mark.asyncio
async def test_an_unreadable_status_at_the_claim_waits_without_the_bound(c1, monkeypatch):
    from infra import carry_in_drain

    await _pending(c1)
    seen = _status_seat(monkeypatch, [None])
    monkeypatch.setattr(carry_in_drain, "_sample", lambda *a: pytest.fail("a sample ran"))
    started = time.monotonic()

    assert await carry_in_drain.wake(c1.workspace) is False
    assert time.monotonic() - started < 5 and len(seen) == 1
    assert _events()[-1]["reason"] == "no_ready_local_model"


@pytest.mark.asyncio
async def test_a_busy_reading_at_dispatch_reruns_the_sample_once_the_seat_goes_quiet(c1, monkeypatch):
    from eval import endpoint, turn_result
    from infra import carry_in_drain

    fact = await _pending(c1)
    # Dispatch proof: quiet, then a short burst. The wait: quiet, quiet. The rerun's dispatch proof: quiet, quiet.
    seen = _status_seat(monkeypatch, [QUIET, BUSY, QUIET, QUIET, QUIET, QUIET])
    dispatched: list = []
    monkeypatch.setattr(endpoint, "run_eval_turn", _admitting_turn(dispatched))
    monkeypatch.setattr(turn_result, "read_turn_result", _read_turn_result("b"))
    created: list = []
    prefix = f"eval-c1-{fact.identity[:16]}"

    passed = await carry_in_drain._sample(fact, OWNER, MODEL, ACTIONS, _arm(c1, fact, deliver=False), "b1", created)

    assert passed is True and len(seen) == 6
    assert dispatched == [f"turn-{prefix}-b1r2"]  # the refused thread never reached the provider
    assert [thread for thread, _user in created] == [f"{prefix}-b1", f"{prefix}-b1r2"]


@pytest.mark.asyncio
async def test_a_seat_that_never_quiets_ends_the_sample_at_the_bound_without_a_call(c1, monkeypatch):
    from eval import endpoint, turn_result
    from infra import carry_in_drain

    fact = await _pending(c1)
    _status_seat(monkeypatch, [BUSY], timeout=0.05)
    dispatched: list = []
    monkeypatch.setattr(endpoint, "run_eval_turn", _admitting_turn(dispatched))
    monkeypatch.setattr(turn_result, "read_turn_result", lambda *_a: pytest.fail("read an undispatched turn"))

    with pytest.raises(carry_in_drain._Unavailable):
        await carry_in_drain._sample(fact, OWNER, MODEL, ACTIONS, _arm(c1, fact, deliver=False), "b1", [])
    assert dispatched == []


@pytest.mark.asyncio
async def test_an_unreadable_status_at_dispatch_ends_the_sample_at_once(c1, monkeypatch):
    from eval import endpoint, turn_result
    from infra import carry_in_drain

    fact = await _pending(c1)
    seen = _status_seat(monkeypatch, [None])
    dispatched: list = []
    monkeypatch.setattr(endpoint, "run_eval_turn", _admitting_turn(dispatched))
    monkeypatch.setattr(turn_result, "read_turn_result", lambda *_a: pytest.fail("read an undispatched turn"))
    started = time.monotonic()

    with pytest.raises(carry_in_drain._Unavailable):
        await carry_in_drain._sample(fact, OWNER, MODEL, ACTIONS, _arm(c1, fact, deliver=False), "b1", [])
    assert time.monotonic() - started < 5 and len(seen) == 2 and dispatched == []


@pytest.mark.asyncio
async def test_a_busy_seat_that_goes_quiet_lets_each_sample_run_and_count(c1, monkeypatch):
    from eval import endpoint, turn_result
    from infra import carry_in_drain

    fact = await _pending(c1)
    seen = _status_seat(monkeypatch, [QUIET])
    dispatched: list = []
    monkeypatch.setattr(endpoint, "run_eval_turn", _admitting_turn(dispatched))
    monkeypatch.setattr(turn_result, "read_turn_result", _read_turn_result("b"))
    completed = [0]

    passes = await carry_in_drain._run_side(
        fact, OWNER, MODEL, ACTIONS, _arm(c1, fact, deliver=False), "b", [], completed,
    )
    assert (passes, completed[0]) == (2, 2) and len(dispatched) == 2
    assert len(seen) == 4  # one two-read proof inside each sample's Turn, no other check
