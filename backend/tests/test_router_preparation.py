"""Real router preparation ordering, worker lifetime, and cancellation contracts."""

from __future__ import annotations

import asyncio
import gc
import threading
import weakref

import pytest

from events import router
from events.connectors import github_route_preparation as prep
from infra.settings import get_setting
from tests.event_routing_helpers import mk_session, pr_payload, register_pr


async def _wait(event):
    assert await asyncio.to_thread(event.wait, 3), "barrier was not reached"


async def _accept(*args, **kwargs):
    return True


@pytest.fixture
def owned(isolated_db, monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")
    owner = mk_session("preparation-owner")
    register_pr("o/r", 1, owner)
    register_pr("o/r", 2, owner)
    monkeypatch.setattr(router.delivery, "deliver_and_resolve_to_thread", _accept)
    return owner


@pytest.mark.parametrize("second_number", [1, 2], ids=["same-source", "distinct-source"])
@pytest.mark.asyncio
async def test_preparation_order_and_delivery_not_serialized(owned, monkeypatch, second_number):
    entered, release = threading.Event(), threading.Event()
    delivery_wait = asyncio.Event()
    delivered_first = asyncio.Event()
    order = []
    original = prep._prepare_route

    def observed(unit, event, payload, *args):
        label = payload["pull_request"]["title"]
        order.append(("start", label))
        if label == "first":
            entered.set()
            assert release.wait(3)
        original(unit, event, payload, *args)
        order.append(("end", label))

    async def delivery(*args, **kwargs):
        if args[4]["pr_title"] == "first":
            delivered_first.set()
            await delivery_wait.wait()
        return True

    monkeypatch.setattr(prep, "_prepare_route", observed)
    monkeypatch.setattr(router.delivery, "deliver_and_resolve_to_thread", delivery)
    first = pr_payload("o/r", 1)
    first["pull_request"]["title"] = "first"
    second = pr_payload("o/r", second_number)
    second["pull_request"]["title"] = "second"
    one = asyncio.create_task(router.route_event("webhook.github.pr_opened", first))
    await _wait(entered)
    two = asyncio.create_task(router.route_event("webhook.github.pr_opened", second))
    await asyncio.sleep(0)
    assert order == [("start", "first")]
    release.set()
    await delivered_first.wait()
    assert (await two).outcome is router.RouteOutcome.ACCEPTED
    assert not one.done(), "preparation gate serialized awaited delivery"
    delivery_wait.set()
    await one
    assert order == [("start", "first"), ("end", "first"), ("start", "second"), ("end", "second")]


@pytest.mark.asyncio
async def test_cancel_waiting_for_gate_makes_no_effect(owned):
    gate = prep._preparation_gate()
    async with gate:
        task = asyncio.create_task(router.route_event("webhook.github.pr_opened", pr_payload("o/r", 1)))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert get_setting(f"pr_notice:{owned}:o/r#1") is None


@pytest.mark.asyncio
async def test_cancel_queued_unstarted_unit_makes_no_effect(owned, monkeypatch):
    from infra import db_async

    submitted, permit = asyncio.Event(), asyncio.Event()
    original = db_async.run_in_db
    calls = []

    async def queued(fn, *args, **kwargs):
        submitted.set()
        await permit.wait()
        calls.append(fn.__name__)
        return await original(fn, *args, **kwargs)

    monkeypatch.setattr(db_async, "run_in_db", queued)
    task = asyncio.create_task(router.route_event("webhook.github.pr_opened", pr_payload("o/r", 1)))
    await submitted.wait()
    task.cancel()
    await asyncio.sleep(0)
    permit.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ["_prepare_route"]
    assert get_setting(f"pr_notice:{owned}:o/r#1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("merged", [False, True])
@pytest.mark.parametrize("worker_error", [False, True])
async def test_cancel_started_unit_joins_releases_notice_and_schedules_reap(
    owned, monkeypatch, merged, worker_error
):
    entered, release = threading.Event(), threading.Event()
    release_entered, release_permit = threading.Event(), threading.Event()
    original, original_release = prep._prepare_route, router.github_pr_notice.release_notice
    reaps, releases, deliveries = [], [], []
    loop = asyncio.get_running_loop()

    def blocked(unit, *args):
        original(unit, *args)
        entered.set()
        assert release.wait(3)
        if worker_error:
            raise RuntimeError("failure after committed preparation")

    def release_notice(*args):
        releases.append(args)
        release_entered.set()
        assert release_permit.wait(3)
        original_release(*args)

    def schedule(payload):
        assert asyncio.get_running_loop() is loop
        reaps.append(payload)

    async def deliver(*args, **kwargs):
        deliveries.append(args)
        return True

    monkeypatch.setattr(prep, "_prepare_route", blocked)
    monkeypatch.setattr(router.github_pr_notice, "release_notice", release_notice)
    monkeypatch.setattr(router, "schedule_merged_pr_reap", schedule)
    monkeypatch.setattr(router.delivery, "deliver_and_resolve_to_thread", deliver)
    payload = pr_payload("o/r", 1, action="closed" if merged else "opened")
    payload["pull_request"]["merged"] = merged
    event = "webhook.github.pr_merged" if merged else "webhook.github.pr_opened"
    task = asyncio.create_task(router.route_event(event, payload))
    await _wait(entered)
    assert (get_setting(f"pr_notice:{owned}:o/r#1") is not None) is (not merged)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    assert not task.done()
    release.set()
    if not merged:
        await _wait(release_entered)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    release_permit.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert get_setting(f"pr_notice:{owned}:o/r#1") is None
    assert len(releases) == (0 if merged else 1)
    assert not deliveries
    assert reaps == ([payload] if merged else [])


@pytest.mark.asyncio
async def test_cancel_after_preparation_releases_notice(owned, monkeypatch):
    entered = asyncio.Event()

    async def delivery(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(router.delivery, "deliver_and_resolve_to_thread", delivery)
    task = asyncio.create_task(router.route_event("webhook.github.pr_opened", pr_payload("o/r", 1)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert get_setting(f"pr_notice:{owned}:o/r#1") is None


def test_contended_gate_does_not_retain_closed_loop():
    loop = asyncio.new_event_loop()
    ref = weakref.ref(loop)

    async def contend():
        gate = prep._preparation_gate()
        async with gate:
            waiter = asyncio.create_task(gate.acquire())
            await asyncio.sleep(0)
            assert gate._loop is loop
        await waiter
        gate.release()

    loop.run_until_complete(contend())
    loop.close()
    del loop
    gc.collect()
    assert ref() is None
    other = asyncio.new_event_loop()
    try:
        async def get_gate():
            return prep._preparation_gate()
        other.run_until_complete(get_gate())
    finally:
        other.close()


@pytest.mark.asyncio
async def test_merged_reap_runs_on_loop_even_when_selection_raises(owned, monkeypatch):
    loop = asyncio.get_running_loop()
    reaps = []

    def broken(*args, **kwargs):
        raise RuntimeError("selection failed")

    def reap(payload):
        assert asyncio.get_running_loop() is loop
        reaps.append(payload)

    monkeypatch.setattr(router, "_select_route", broken)
    monkeypatch.setattr(router, "schedule_merged_pr_reap", reap)
    payload = pr_payload("o/r", 1, action="closed")
    payload["pull_request"]["merged"] = True
    with pytest.raises(RuntimeError, match="selection failed"):
        await router.route_event("webhook.github.pr_merged", payload)
    assert reaps == [payload]


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["normal", "missing", "noise", "cancelled", "resolution-error", "merged"])
async def test_real_route_branches_have_no_loop_writes(owned, monkeypatch, branch):
    from events.identity import PrResolutionError
    from infra import worktree_reaper_pr

    monkeypatch.setenv("CORSIA_DB_LOOP_WRITE_STRICT", "1")
    payload = pr_payload("o/r", 1)
    event = "webhook.github.pr_opened"
    expected = router.RouteOutcome.ACCEPTED
    if branch == "missing":
        payload = pr_payload("absent/repo", 5)
        expected = router.RouteOutcome.DEAD_LETTERED
    elif branch in {"noise", "cancelled", "resolution-error"}:
        event = "webhook.github.check_suite_completed"
        payload = {"repository": {"full_name": "o/r"}, "check_suite": {
            "conclusion": "cancelled" if branch == "cancelled" else "success",
            "pull_requests": [], "head_branch": "dev",
        }}
        expected = (router.RouteOutcome.SUPPRESSED if branch == "cancelled"
                    else router.RouteOutcome.PARKED)
        if branch == "resolution-error":
            def fail(*args):
                raise PrResolutionError("synthetic metadata failure")
            monkeypatch.setattr(router, "check_suite_pr_metadata_from_event", fail)
            expected = router.RouteOutcome.DEAD_LETTERED
    elif branch == "merged":
        event = "webhook.github.pr_merged"
        payload["action"] = "closed"
        payload["pull_request"]["merged"] = True
        expected = router.RouteOutcome.ACCEPTED
        called = threading.Event()
        monkeypatch.setattr(worktree_reaper_pr, "reap_merged_pr", lambda *a, **kw: called.set())
    result = await router.route_event(event, payload)
    assert result.outcome is expected
    if branch == "merged":
        await asyncio.gather(*list(worktree_reaper_pr._PENDING))
        assert called.is_set(), "actual loop-owned reap task was not run"


@pytest.mark.asyncio
async def test_admission_drain_waits_for_cancelled_preparation_cleanup(owned, monkeypatch):
    from events.connectors.github_admission import admit_github_delivery_async
    from events.connectors.github_webhook import _BG_DISPATCH
    from infra import drain

    entered, release = threading.Event(), threading.Event()
    original = prep._prepare_route
    initial_admissions = drain._turn_admissions

    def blocked(unit, *args):
        original(unit, *args)
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(prep, "_prepare_route", blocked)
    outcome = await admit_github_delivery_async("pull_request", pr_payload("o/r", 1), "drain-proof")
    assert outcome.status_code == 200
    await _wait(entered)
    dispatches = list(_BG_DISPATCH)
    assert dispatches
    assert drain._turn_admissions > initial_admissions
    for dispatch in dispatches:
        dispatch.cancel()
    await asyncio.sleep(0)
    assert drain._turn_admissions > initial_admissions
    assert any(not task.done() for task in dispatches)
    release.set()
    await asyncio.gather(*dispatches, return_exceptions=True)
    await asyncio.sleep(0)
    assert drain._turn_admissions == initial_admissions
    assert get_setting(f"pr_notice:{owned}:o/r#1") is None
