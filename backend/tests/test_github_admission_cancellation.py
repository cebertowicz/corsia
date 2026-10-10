"""A recorded GitHub delivery is always scheduled and settled, even when its caller is cancelled.

The delivery log answers a redelivery of a recorded id with ``duplicate``, and
restart replay never picks up a ``received`` row, so a delivery left
``received`` with nothing scheduled is lost for good.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

import events.connectors.github_admission as admission_module
import events.connectors.github_webhook as webhook_module
from events.connectors.github_admission import admit_github_delivery_async
from infra import drain, webhook_delivery_log
from test_github_raw_facts_at_admission import _deliveries, _facts, _hold_new_work, delivery
from test_webhook_endpoint import _direct_webhook_request
from webhook_ingress_fixtures import WEBHOOK_SECRET


class _Scheduler:
    """Stands in for the dispatch task: records the call and settles at once."""

    def __init__(self, failure: Exception | None = None) -> None:
        self.calls: list[str | None] = []
        self.failure = failure

    def __call__(self, event_type, payload, *, delivery_id=None, on_settled=None):
        self.calls.append(delivery_id)
        if self.failure is not None:
            raise self.failure
        if event_type != "pull_request":
            return None
        if on_settled is not None:
            on_settled()
        return "github.pr.opened"


@pytest.fixture(autouse=True)
def _own_admission_count(monkeypatch):
    """Each probe counts only its own admissions, so one failure cannot fail the next."""
    monkeypatch.setattr(drain, "_turn_admissions", 0)


@pytest.fixture
def scheduler(monkeypatch):
    stub = _Scheduler()
    monkeypatch.setattr(webhook_module, "schedule_github_webhook", stub)
    return stub


BARRIER_SECONDS = 5.0


async def _reached(barrier: threading.Event) -> None:
    """Wait on the loop for a worker-side barrier, failing instead of hanging."""
    reached = await asyncio.wait_for(
        asyncio.to_thread(barrier.wait, BARRIER_SECONDS), BARRIER_SECONDS + 1,
    )
    assert reached, "the worker never reached the barrier"


async def _loop_turns() -> None:
    """Yield to the loop so a just-requested cancellation is delivered; not a timed wait."""
    for _ in range(3):
        await asyncio.sleep(0)


@pytest.fixture
def after_commit(monkeypatch):
    """Hold the DB worker just after the record transaction commits."""
    committed, release = threading.Event(), threading.Event()
    real = admission_module.record_delivery_for_admission

    def recorded_then_held(*args: Any, **kwargs: Any):
        result = real(*args, **kwargs)
        committed.set()
        release.wait(BARRIER_SECONDS)
        return result

    monkeypatch.setattr(admission_module, "record_delivery_for_admission", recorded_then_held)
    yield committed, release
    release.set()


async def _cancel_while_held(
    task: asyncio.Task, held: threading.Event, release: threading.Event, *, times: int = 1,
) -> None:
    """Cancel the caller while its worker is held, then let the worker finish."""
    try:
        await _reached(held)
        for _ in range(times):
            task.cancel()
            await _loop_turns()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, BARRIER_SECONDS)


async def _cancel_after_commit(after_commit, delivery_id: str, *, times: int) -> None:
    committed, release = after_commit
    event_type, payload = delivery("pr_opened")
    task = asyncio.create_task(admit_github_delivery_async(event_type, payload, delivery_id))
    await _cancel_while_held(task, committed, release, times=times)


@pytest.mark.parametrize("times", [1, 3])
async def test_cancelling_the_caller_after_the_record_commit_still_schedules_and_settles(
    main_db, scheduler, after_commit, times,
):
    await _cancel_after_commit(after_commit, "d-cancelled", times=times)

    assert _facts(main_db), "the record transaction committed its facts"
    assert scheduler.calls == ["d-cancelled"], "a recorded delivery is scheduled exactly once"
    assert _deliveries(main_db) == {"d-cancelled": "dispatched"}
    assert drain._turn_admissions == 0, "the admission is released"


async def test_cancelling_the_caller_during_the_settle_write_still_settles(
    main_db, scheduler, monkeypatch,
):
    entered, release = threading.Event(), threading.Event()
    real = webhook_delivery_log.update_delivery_status

    def held_update(*args: Any, **kwargs: Any):
        entered.set()
        release.wait(BARRIER_SECONDS)
        return real(*args, **kwargs)

    monkeypatch.setattr(webhook_delivery_log, "update_delivery_status", held_update)
    event_type, payload = delivery("pr_opened")
    task = asyncio.create_task(admit_github_delivery_async(event_type, payload, "d-settling"))
    await _cancel_while_held(task, entered, release)

    assert _deliveries(main_db) == {"d-settling": "dispatched"}
    assert drain._turn_admissions == 0


async def test_a_dispatcher_failure_before_settle_is_kept_as_failed(main_db, monkeypatch):
    monkeypatch.setattr(
        webhook_module, "schedule_github_webhook", _Scheduler(RuntimeError("no dispatcher")),
    )
    event_type, payload = delivery("pr_opened")

    with pytest.raises(RuntimeError):
        await admit_github_delivery_async(event_type, payload, "d-failed")

    assert _deliveries(main_db) == {"d-failed": "failed"}
    assert _facts(main_db), "the facts of a delivery that failed to dispatch are kept"
    assert drain._turn_admissions == 0


async def test_a_scheduled_dispatch_that_fails_before_the_settle_write_stays_failed(
    main_db, monkeypatch,
):
    """A real scheduled task marks the delivery failed while the settle write is still queued.

    No production dispatch task writes a delivery status today; the task here stands for
    any later owner that does. The settle write must lose that race, not overwrite it.
    """
    failed_written = threading.Event()
    dispatches: list[asyncio.Task] = []
    update = webhook_delivery_log.update_delivery_status

    def schedule(event_type, payload, *, delivery_id=None, on_settled=None):
        async def fail_first() -> None:
            try:
                await asyncio.to_thread(
                    update, delivery_id, "failed", "leg failed", expected_status="received",
                )
            finally:
                failed_written.set()
            raise RuntimeError("dispatch failed before the settle write")

        task = asyncio.create_task(fail_first())
        task.add_done_callback(
            lambda done: webhook_module._settle_dispatch(done, on_settled=on_settled)
        )
        dispatches.append(task)
        return "github.pr.opened"

    real_settle = admission_module._settle_phase
    settle_saw_failed: list[bool] = []

    def settle_after_the_failure(*args: Any, **kwargs: Any):
        settle_saw_failed.append(failed_written.wait(BARRIER_SECONDS))
        return real_settle(*args, **kwargs)

    monkeypatch.setattr(webhook_module, "schedule_github_webhook", schedule)
    monkeypatch.setattr(admission_module, "_settle_phase", settle_after_the_failure)
    event_type, payload = delivery("pr_opened")

    try:
        await asyncio.wait_for(
            admit_github_delivery_async(event_type, payload, "d-lost-race"), BARRIER_SECONDS,
        )
    finally:
        failed_written.set()
    await asyncio.wait(dispatches, timeout=BARRIER_SECONDS)
    await asyncio.sleep(0)  # let the dispatch task's done callback release the admission

    assert settle_saw_failed == [True], "the failure was written before the settle write ran"
    assert _deliveries(main_db) == {"d-lost-race": "failed"}, "settle did not overwrite it"
    assert _facts(main_db), "the facts of a delivery that failed to dispatch are kept"
    assert drain._turn_admissions == 0, "the dispatch task, not the admission, released it"


async def test_a_dispatcher_failure_never_overwrites_a_dispatched_delivery(
    main_db, monkeypatch,
):
    """The reverse order: the settle write lands first, then a real scheduled task fails."""
    settled = asyncio.Event()
    dispatches: list[asyncio.Task] = []
    failure_applied: list[bool] = []
    update = webhook_delivery_log.update_delivery_status

    def schedule(event_type, payload, *, delivery_id=None, on_settled=None):
        async def fail_after_settle() -> None:
            await asyncio.wait_for(settled.wait(), BARRIER_SECONDS)
            failure_applied.append(await asyncio.to_thread(
                update, delivery_id, "failed", "leg failed", expected_status="received",
            ))
            raise RuntimeError("dispatch failed after the settle write")

        task = asyncio.create_task(fail_after_settle())
        task.add_done_callback(
            lambda done: webhook_module._settle_dispatch(done, on_settled=on_settled)
        )
        dispatches.append(task)
        return "github.pr.opened"

    monkeypatch.setattr(webhook_module, "schedule_github_webhook", schedule)
    event_type, payload = delivery("pr_opened")

    try:
        outcome = await asyncio.wait_for(
            admit_github_delivery_async(event_type, payload, "d-raced"), BARRIER_SECONDS,
        )
        assert _deliveries(main_db) == {"d-raced": "dispatched"}
        assert drain._turn_admissions == 1, "the running dispatch task still owns the admission"
    finally:
        settled.set()
    await asyncio.wait(dispatches, timeout=BARRIER_SECONDS)
    await _loop_turns()

    assert outcome.content["status"] == "ok"
    assert failure_applied == [False], "the late failure lost the CAS"
    assert _deliveries(main_db) == {"d-raced": "dispatched"}
    assert _facts(main_db)
    assert drain._turn_admissions == 0


def _unit_task(delivery_id: str) -> asyncio.Task:
    (unit,) = [
        task for task in asyncio.all_tasks()
        if task.get_name() == f"github-admission:{delivery_id}"
    ]
    return unit


async def test_a_unit_cancelled_before_its_first_step_releases_the_admission(
    main_db, scheduler,
):
    event_type, payload = delivery("pr_opened")
    caller = asyncio.create_task(admit_github_delivery_async(event_type, payload, "d-unstarted"))
    await asyncio.sleep(0)  # the caller has created its unit; the unit has not run yet
    assert drain._turn_admissions == 1, "the admission is taken before the unit exists"

    _unit_task("d-unstarted").cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(caller, BARRIER_SECONDS)
    assert _deliveries(main_db) == {}, "nothing was recorded"
    assert scheduler.calls == []
    assert drain._turn_admissions == 0, "the wrapper released what the unit never owned"


async def test_a_unit_cancelled_in_its_record_phase_releases_the_admission_once(
    main_db, scheduler, after_commit,
):
    """Loop teardown is the only thing that cancels the unit itself; the named residual."""
    committed, release = after_commit
    event_type, payload = delivery("pr_opened")
    caller = asyncio.create_task(admit_github_delivery_async(event_type, payload, "d-torn-down"))
    try:
        await _reached(committed)
        _unit_task("d-torn-down").cancel()
        await _loop_turns()
    finally:
        release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(caller, BARRIER_SECONDS)
    assert _deliveries(main_db) == {"d-torn-down": "received"}, (
        "the accepted residual: recorded with its facts, never scheduled"
    )
    assert _facts(main_db), "the raw facts are durable"
    assert scheduler.calls == []
    assert drain._turn_admissions == 0


async def test_a_unit_that_fails_while_its_caller_is_cancelled_is_logged_and_consumed(
    main_db, monkeypatch, after_commit, caplog,
):
    import gc

    committed, release = after_commit
    monkeypatch.setattr(
        webhook_module, "schedule_github_webhook", _Scheduler(RuntimeError("no dispatcher")),
    )
    unhandled: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    event_type, payload = delivery("pr_opened")
    try:
        caller = asyncio.create_task(
            admit_github_delivery_async(event_type, payload, "d-failed-cancelled"),
        )
        with caplog.at_level("ERROR", logger=admission_module.__name__):
            await _cancel_while_held(caller, committed, release)
        del caller
        gc.collect()
        await _loop_turns()
    finally:
        loop.set_exception_handler(previous)

    assert unhandled == [], "no task exception was left unretrieved"
    assert any("d-failed-cancelled" in record.getMessage() for record in caplog.records), (
        "the failure the caller could not receive is logged"
    )
    cancelled_logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "RuntimeError" in cancelled_logged, (
        "the class names the failure for whoever reads the log later"
    )
    assert "no dispatcher" not in cancelled_logged, (
        "only the exception class is logged: its text can carry a payload value"
    )
    assert _deliveries(main_db) == {"d-failed-cancelled": "failed"}
    assert drain._turn_admissions == 0


async def test_a_drain_started_during_the_record_phase_waits_for_schedule_and_settle(
    main_db, monkeypatch, after_commit,
):
    from automation import self_restart

    committed, release = after_commit
    dispatch_may_finish = asyncio.Event()
    dispatches: list[asyncio.Task] = []
    ready: dict[str, bool] = {}

    def schedule(event_type, payload, *, delivery_id=None, on_settled=None):
        ready["at schedule"] = drain.check_drain_ready()

        async def legs() -> None:
            await asyncio.wait_for(dispatch_may_finish.wait(), BARRIER_SECONDS)

        task = asyncio.create_task(legs())
        task.add_done_callback(
            lambda done: webhook_module._settle_dispatch(done, on_settled=on_settled)
        )
        dispatches.append(task)
        return "github.pr.opened"

    real_settle = admission_module._settle_phase

    def settle(*args: Any, **kwargs: Any):
        ready["at settle"] = drain.check_drain_ready()
        return real_settle(*args, **kwargs)

    monkeypatch.setattr(webhook_module, "schedule_github_webhook", schedule)
    monkeypatch.setattr(admission_module, "_settle_phase", settle)
    event_type, payload = delivery("pr_opened")
    caller = asyncio.create_task(admit_github_delivery_async(event_type, payload, "d-draining"))
    try:
        await _reached(committed)
        # The restart is requested only now, so the delivery was recorded as live work.
        monkeypatch.setattr(self_restart, "in_flight_request", lambda: {"id": "restart-1"})
        ready["record held"] = drain.check_drain_ready()
    finally:
        release.set()
    try:
        outcome = await asyncio.wait_for(caller, BARRIER_SECONDS)
        ready["dispatch running"] = drain.check_drain_ready()
    finally:
        dispatch_may_finish.set()
    await asyncio.wait(dispatches, timeout=BARRIER_SECONDS)
    await _loop_turns()

    assert outcome.content["status"] == "ok"
    assert ready == {
        "record held": False, "at schedule": False,
        "at settle": False, "dispatch running": False,
    }, "the drain is never ready while the delivery is unscheduled, unsettled or dispatching"
    assert _deliveries(main_db) == {"d-draining": "dispatched"}
    assert drain.check_drain_ready(), "ready once every leg settled"


class _RealDispatch:
    """The real ``schedule_github_webhook`` and dispatch task; only the three legs are faked."""

    def __init__(self, monkeypatch) -> None:
        import events.ingress as ingress

        self.route_failure: Exception | None = None
        self.legs_may_finish: asyncio.Event | None = None
        self.routed: list[str | None] = []

        async def route_leg(internal_event, payload, delivery_id=None):
            self.routed.append(delivery_id)
            if self.legs_may_finish is not None:
                await asyncio.wait_for(self.legs_may_finish.wait(), BARRIER_SECONDS)
            if self.route_failure is not None:
                raise self.route_failure
            return None

        monkeypatch.setattr(ingress, "route_leg", route_leg)
        monkeypatch.setattr(ingress, "review_leg", lambda *_args: True)
        monkeypatch.setattr(ingress, "gate_trigger_leg", lambda *_args: True)

    async def finished(self) -> None:
        """Wait until every dispatch task and its done callback has run."""
        tasks = list(webhook_module._BG_DISPATCH)
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), BARRIER_SECONDS)
        await _loop_turns()
        assert not webhook_module._BG_DISPATCH, "a dispatch task is still running"


@pytest.fixture
def real_dispatch(monkeypatch):
    return _RealDispatch(monkeypatch)


@pytest.fixture
def held_settle(monkeypatch):
    """Hold the DB worker at the start of the settle write."""
    settling, release = threading.Event(), threading.Event()
    real = admission_module._settle_phase

    def held(*args: Any, **kwargs: Any):
        settling.set()
        release.wait(BARRIER_SECONDS)
        return real(*args, **kwargs)

    monkeypatch.setattr(admission_module, "_settle_phase", held)
    yield settling, release
    release.set()


async def test_a_real_dispatch_that_finishes_before_the_settle_write_does_not_free_the_drain(
    main_db, monkeypatch, real_dispatch, held_settle,
):
    from automation import self_restart

    settling, release = held_settle
    event_type, payload = delivery("pr_opened")
    caller = asyncio.create_task(admit_github_delivery_async(event_type, payload, "d-early"))
    try:
        await _reached(settling)
        await real_dispatch.finished()
        monkeypatch.setattr(self_restart, "in_flight_request", lambda: {"id": "restart-1"})
        status_while_settling = _deliveries(main_db)
        count_while_settling = drain._turn_admissions
        ready_while_settling = drain.check_drain_ready()
    finally:
        release.set()
    outcome = await asyncio.wait_for(caller, BARRIER_SECONDS)

    assert real_dispatch.routed == ["d-early"], "the real dispatcher ran its route leg"
    assert status_while_settling == {"d-early": "received"}, "the settle write had not landed"
    assert count_while_settling == 1, "the unit still holds its admission while it settles"
    assert not ready_while_settling, "the drain was ready with the settle write still running"
    assert outcome.content["status"] == "ok"
    assert _deliveries(main_db) == {"d-early": "dispatched"}
    assert drain._turn_admissions == 0
    assert drain.check_drain_ready(), "ready once the dispatch and the unit are both done"


async def test_a_failed_real_dispatch_and_a_caller_cancelled_mid_settle_still_hold_the_drain(
    main_db, real_dispatch, held_settle,
):
    settling, release = held_settle
    real_dispatch.route_failure = RuntimeError("route leg failed")
    event_type, payload = delivery("pr_opened")
    caller = asyncio.create_task(admit_github_delivery_async(event_type, payload, "d-early-failed"))
    try:
        await _reached(settling)
        await real_dispatch.finished()
        caller.cancel()
        await _loop_turns()
        count_while_settling = drain._turn_admissions
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(caller, BARRIER_SECONDS)

    assert count_while_settling == 1, "a cancelled caller and a finished dispatch free nothing"
    assert _deliveries(main_db) == {"d-early-failed": "dispatched"}, (
        "a failed leg is logged by the dispatcher; it does not change the delivery status"
    )
    assert _facts(main_db)
    assert drain._turn_admissions == 0


async def test_the_unit_and_the_real_dispatch_each_hold_their_own_admission(
    main_db, real_dispatch, held_settle,
):
    settling, release = held_settle
    real_dispatch.legs_may_finish = asyncio.Event()
    event_type, payload = delivery("pr_opened")
    caller = asyncio.create_task(admit_github_delivery_async(event_type, payload, "d-both"))
    try:
        try:
            await _reached(settling)
            both_live = drain._turn_admissions
        finally:
            release.set()
        await asyncio.wait_for(caller, BARRIER_SECONDS)
        dispatch_only = drain._turn_admissions
    finally:
        real_dispatch.legs_may_finish.set()
    await real_dispatch.finished()

    assert both_live == 2, "unit settling and dispatch running"
    assert dispatch_only == 1, "unit done, dispatch still running"
    assert drain._turn_admissions == 0
    assert _deliveries(main_db) == {"d-both": "dispatched"}


async def test_a_refused_unit_task_releases_the_admission_and_leaves_no_coroutine(
    main_db, monkeypatch,
):
    import gc
    import warnings

    real_create_task = asyncio.create_task

    def refuse_the_unit(coro, **kwargs: Any):
        if str(kwargs.get("name", "")).startswith("github-admission:"):
            raise RuntimeError("the loop is closing")
        return real_create_task(coro, **kwargs)

    monkeypatch.setattr(asyncio, "create_task", refuse_the_unit)
    event_type, payload = delivery("pr_opened")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(RuntimeError, match="the loop is closing"):
            await admit_github_delivery_async(event_type, payload, "d-refused")
        gc.collect()

    assert [str(w.message) for w in caught if "never awaited" in str(w.message)] == []
    assert _deliveries(main_db) == {}
    assert drain._turn_admissions == 0



async def test_an_event_the_real_scheduler_does_not_map_is_ignored_and_frees_the_drain(
    main_db, real_dispatch,
):
    outcome = await admit_github_delivery_async("deployment", {"action": "created"}, "d-unmapped")

    assert outcome.content["status"] == "ignored"
    assert real_dispatch.routed == [], "nothing was dispatched"
    assert not webhook_module._BG_DISPATCH
    assert _deliveries(main_db) == {"d-unmapped": "ignored"}
    assert drain._turn_admissions == 0, "no dispatch task exists to release the second admission"


async def test_a_payload_the_real_scheduler_cannot_map_is_refused_and_frees_the_drain(
    main_db, real_dispatch,
):
    # A bot comment whose body is not text makes the real event mapper raise.
    poison = {
        "action": "created",
        "issue": {"number": 7, "pull_request": {"url": "u"}},
        "comment": {"id": 71, "body": 5},
        "sender": {"login": "ci[bot]", "type": "Bot"},
        "repository": {"full_name": "Acme/Widgets"},
    }

    outcome = await admit_github_delivery_async("issue_comment", poison, "d-poison")

    assert outcome.status_code == 422
    assert outcome.content["status"] == "error"
    assert real_dispatch.routed == []
    assert _deliveries(main_db) == {"d-poison": "failed"}
    assert drain._turn_admissions == 0
