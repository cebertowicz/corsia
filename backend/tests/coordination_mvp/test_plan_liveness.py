"""Exact-once structured plan-liveness wake and sweep behavior."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from automation import plan_liveness as pl
from infra.database import get_db
from plans import admission, mutations, projections, service
from tests.event_routing_helpers import mk_session
from tests.plan_review_helpers import submit_review_evidence

_PAST = "2000-01-01T00:00:00+00:00"
_FUTURE = "2099-01-01T00:00:00+00:00"


def _plan(thread_id: str, *, tasks: int = 1):
    db = get_db()
    plan = service.ensure_plan("thread", thread_id, db=db)
    nodes = [
        service.add_node(
            plan.id,
            kind="task",
            title=f"Task {index}",
            acceptance_criteria=f"Task {index} tests pass",
            db=db,
        )
        for index in range(tasks)
    ]
    nodes = [service.transition_node(node.id, "active", db=db) for node in nodes]
    admission.reconcile_plan(
        plan.id, actor_id=thread_id, provenance="test", db=db,
    )
    return db, plan, nodes


def _arm(thread_id: str, *, tasks: int = 1):
    db, plan, nodes = _plan(thread_id, tasks=tasks)
    wake = pl.on_turn_complete(thread_id, db=db)
    db.execute(
        "UPDATE plan_liveness_wakes SET deadline=? WHERE thread_id=?",
        (_PAST, thread_id),
    )
    db.commit()
    return db, plan, nodes, wake


def _armed(thread_id: str, db):
    return db.execute(
        "SELECT * FROM plan_liveness_wakes WHERE thread_id=?", (thread_id,)
    ).fetchone()


def _watch(thread_id: str, db) -> None:
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """INSERT INTO expected_event_watches
           (id,owner_thread_id,condition_type,status,armed_at,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (f"watch:{thread_id}", thread_id, "manual", "armed", now, now, now),
    )
    db.commit()


class TestTurnComplete:
    def test_actionable_plan_arms_and_repeated_hook_is_idempotent(self, isolated_db):
        thread_id = mk_session("structured-open")
        db, _plan_row, _nodes = _plan(thread_id, tasks=2)

        first = pl.on_turn_complete(thread_id, db=db)
        second = pl.on_turn_complete(thread_id, db=db)

        assert first.id == second.id
        assert second.last_open_count == 1
        assert db.execute(
            "SELECT COUNT(*) FROM plan_liveness_wakes WHERE thread_id=?",
            (thread_id,),
        ).fetchone()[0] == 1

    def test_no_plan_or_no_actionable_work_cancels(self, isolated_db):
        no_plan = mk_session("structured-none")
        assert pl.on_turn_complete(no_plan) is None

        done_thread = mk_session("structured-done")
        db, _plan_row, nodes = _plan(done_thread)
        wake = pl.on_turn_complete(done_thread, db=db)
        submit_review_evidence(nodes[0].id, db=db)
        service.transition_node(
            nodes[0].id,
            "accepted",
            verification_result="passed",
            verified_by="reviewer",
            db=db,
        )

        assert pl.on_turn_complete(done_thread, db=db) is None
        assert _armed(done_thread, db)["status"] == "cancelled"
        assert wake is not None

    def test_a_watch_not_on_the_actionable_task_does_not_quiet_it(self, isolated_db):
        thread_id = mk_session("structured-watch")
        db, _plan_row, _nodes = _plan(thread_id)
        assert pl.on_turn_complete(thread_id, db=db) is not None
        _watch(thread_id, db)

        assert pl.on_turn_complete(thread_id, db=db) is not None
        assert _armed(thread_id, db)["status"] == "armed"

    def test_equal_count_hash_change_does_not_reset_cycle(self, isolated_db, monkeypatch):
        thread_id = mk_session("structured-progress")
        db, _plan_row, nodes = _plan(thread_id, tasks=2)
        first_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        monkeypatch.setattr(pl, "_now", lambda: first_now)
        first = pl.on_turn_complete(thread_id, db=db)
        db.execute(
            "UPDATE plan_liveness_wakes SET cycle=2 WHERE thread_id=?",
            (thread_id,),
        )
        db.commit()

        mutations.edit_node(nodes[0].id, title="Restated Task", db=db)
        monkeypatch.setattr(
            pl, "_now", lambda: first_now + timedelta(seconds=30)
        )
        second = pl.on_turn_complete(thread_id, db=db)

        assert second.id == first.id
        assert second.cycle == 2
        assert second.last_open_count == first.last_open_count
        assert second.last_signal_hash == first.last_signal_hash


class TestSweep:
    @pytest.mark.asyncio
    async def test_due_wake_redrives_and_rearms(self, isolated_db):
        thread_id = mk_session("structured-drive")
        db, _plan_row, _nodes, _wake = _arm(thread_id)
        calls = []

        async def deliver(*args):
            calls.append(args)
            return True

        result = await pl.sweep_plan_wakes(
            _FUTURE, db=db, is_idle=lambda _tid: True, deliver=deliver
        )

        assert result["woken"] == [thread_id]
        assert len(calls) == 1
        assert calls[0][1] == "plan.liveness_wake"
        assert calls[0][3]["actionable_count"] == 1
        assert _armed(thread_id, db)["status"] == "armed"
        assert _armed(thread_id, db)["cycle"] == 1

    @pytest.mark.asyncio
    async def test_sweep_requeries_signal_and_cancels_completed_plan(self, isolated_db):
        thread_id = mk_session("structured-complete")
        db, _plan_row, nodes, _wake = _arm(thread_id)
        submit_review_evidence(nodes[0].id, db=db)
        service.transition_node(
            nodes[0].id,
            "accepted",
            verification_result="passed",
            verified_by="reviewer",
            db=db,
        )
        calls = []

        result = await pl.sweep_plan_wakes(
            _FUTURE,
            db=db,
            is_idle=lambda _tid: True,
            deliver=lambda *args: calls.append(args),
        )

        assert result["cancelled"] == [thread_id]
        assert calls == []
        assert _armed(thread_id, db)["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_an_unrelated_watch_does_not_cancel_a_due_wake(self, isolated_db):
        thread_id = mk_session("structured-sweep-watch")
        db, _plan_row, _nodes, _wake = _arm(thread_id)
        _watch(thread_id, db)
        calls = []

        result = await pl.sweep_plan_wakes(
            _FUTURE,
            db=db,
            is_idle=lambda _tid: True,
            deliver=lambda *args: calls.append(args),
        )

        assert thread_id not in result["cancelled"]
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_busy_thread_rearms_without_delivery(self, isolated_db):
        thread_id = mk_session("structured-busy")
        db, _plan_row, _nodes, _wake = _arm(thread_id)
        calls = []

        result = await pl.sweep_plan_wakes(
            _FUTURE,
            db=db,
            is_idle=lambda _tid: False,
            deliver=lambda *args: calls.append(args),
        )

        assert result["skipped"] == [thread_id]
        assert calls == []
        assert _armed(thread_id, db)["status"] == "armed"
        assert _armed(thread_id, db)["deadline"] > _PAST

    @pytest.mark.asyncio
    async def test_unchanged_signal_stalls_at_bounded_cycle(self, isolated_db):
        thread_id = mk_session("structured-stall")
        db, _plan_row, _nodes, _wake = _arm(thread_id)
        db.execute(
            "UPDATE plan_liveness_wakes SET cycle=? WHERE thread_id=?",
            (pl.PLAN_WAKE_MAX_CYCLES - 1, thread_id),
        )
        db.commit()
        calls = []

        result = await pl.sweep_plan_wakes(
            _FUTURE,
            db=db,
            is_idle=lambda _tid: True,
            deliver=lambda *args: calls.append(args),
        )

        assert result["stalled"] == [thread_id]
        assert calls == []
        assert _armed(thread_id, db)["status"] == "stalled"
        # An unrelated Turn over the same unchanged state never re-arms the stall.
        assert pl.on_turn_complete(thread_id, db=db) is None
        assert _armed(thread_id, db)["status"] == "stalled"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", ["exception", "false"])
    async def test_delivery_failure_stalls_same_wake(
        self, isolated_db, failure
    ):
        thread_id = mk_session(f"structured-delivery-{failure}")
        db, _plan_row, _nodes, wake = _arm(thread_id)

        async def deliver(*_args):
            if failure == "exception":
                raise RuntimeError("transport failed")
            return False

        result = await pl.sweep_plan_wakes(
            _FUTURE, db=db, is_idle=lambda _tid: True, deliver=deliver
        )
        current = _armed(thread_id, db)

        assert result["errored"] == [thread_id]
        assert current["id"] == wake.id
        assert current["status"] == "stalled"
        assert current["cycle"] == wake.cycle

    @pytest.mark.asyncio
    async def test_concurrent_sweeps_deliver_once(self, isolated_db):
        thread_id = mk_session("structured-exact-once")
        db, _plan_row, _nodes, _wake = _arm(thread_id)
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def deliver(*_args):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return True

        first = asyncio.create_task(
            pl.sweep_plan_wakes(
                _FUTURE, db=db, is_idle=lambda _tid: True, deliver=deliver
            )
        )
        await entered.wait()
        second = asyncio.create_task(
            pl.sweep_plan_wakes(
                _FUTURE, db=db, is_idle=lambda _tid: True, deliver=deliver
            )
        )
        # The sweep awaits its system phase before collecting, so let the second
        # sweep finish while the first still holds the claim. A second delivery
        # would block on `release` and never finish.
        await asyncio.wait({second}, timeout=5)
        release.set()
        await asyncio.gather(first, second)

        assert calls == 1
        assert _armed(thread_id, db)["status"] == "armed"


def test_signal_resolver_is_structured_projection(isolated_db):
    thread_id = mk_session("structured-projection")
    db, plan, _nodes = _plan(thread_id)

    signal = projections.liveness_signal(thread_id, db=db)

    assert signal.plan_id == plan.id
    assert signal.count == 1
