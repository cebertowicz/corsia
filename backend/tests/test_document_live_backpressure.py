"""Owed publications bound lane admission; a waiting write stays queued, never started.

The lane, store, publisher and its failure budget are real; only the
application registration that ``publish_one`` performs is replaced, so a test
can stall, fail or release each owed row.
"""
import asyncio
import threading

import pytest

from doceditor.live_publication_record import Publication, WriteActor
from tests.test_document_live_lane import insert, open_lane

USER = WriteActor("ui", None, direct_user=True)
AGENT = WriteActor("agent-thread", "project-alpha")


class Registration:
    """Stand-in for ``publish_one``: each row waits for release, then succeeds or fails."""

    def __init__(self, *, fail: str | None = None, stall: bool = False):
        self.fail = fail
        self.open = threading.Event()
        if not stall:
            self.open.set()
        self.published: list[str] = []

    def __call__(self, store, row, **kwargs):
        self.open.wait(10)
        if self.fail:
            raise ValueError(self.fail)
        self.published.append(row.publication.actor)
        store.delete_publication(row.seq)


async def gated(tmp_path, monkeypatch, registration, *, bound=3, wait=5.0):
    from doceditor import live_backpressure, live_publisher
    from doceditor.live_lane import DocumentLane
    from doceditor.live_document import LiveDocument
    from doceditor.document_save import write_atomic_plain
    if bound is not None:
        monkeypatch.setattr(live_backpressure, "OWED_ROW_BOUND", bound)
        monkeypatch.setattr(live_backpressure, "DRAIN_WAIT_S", wait)
    monkeypatch.setattr(live_publisher, "publish_one", registration)
    path = tmp_path / "document.md"
    path.write_text("start")
    live = await LiveDocument.open(path, tmp_path / "state.sqlite")
    publisher = live_publisher.DocumentPublisher(live.store)
    publisher.delay = 0.01
    gate = live_backpressure.PublicationGate(publisher)

    async def policy(operation, text):
        mirror = await asyncio.to_thread(live.store.mirror)
        return Publication(operation.actor.thread_id, None, operation.kind, mirror.state,
                           {"path": str(path), "plain": True})

    async def writer(text, prior_hash):
        await asyncio.to_thread(write_atomic_plain, path, text, prior_hash)

    async def valid(text, actor):
        return None

    lane = DocumentLane(live, validate_actor=valid, write=writer, policy=policy,
                        published=publisher.notify, admission=gate)
    return live, lane, publisher, gate


def owed_count(live) -> int:
    return live.store.owed_count()


def operation(index: int):
    from doceditor.live_lane import Operation
    if index % 2:
        return Operation("replace", content=f"agent {index}", actor=AGENT)
    return Operation("flush", actor=USER)


async def until(predicate, timeout=3.0):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(poll(), timeout)


async def close(lane, publisher, registration):
    registration.open.set()
    await lane.close()
    await publisher.close()


@pytest.mark.asyncio
async def test_alternating_writes_with_blocked_publisher_stay_bounded_and_refuse(tmp_path, monkeypatch):
    from doceditor import live_publisher
    registration = Registration(fail="registration permanently refused", stall=True)
    live, lane, publisher, gate = await gated(tmp_path, monkeypatch, registration, bound=3)
    peak, results, acked = 0, [], []
    live.acknowledge = acked.extend
    try:
        for index in range(3):
            if index % 2 == 0:
                insert(live, f"user-{index}", f"U{index} ")
            results.append(await lane.submit(operation(index)))
            peak = max(peak, owed_count(live))
        waiting = asyncio.create_task(lane.submit(operation(3)))
        await until(lambda: gate.waiting == 1)
        registration.open.set()
        result = await asyncio.wait_for(waiting, 3)
        assert live.store.failure("publication_failed").attempts == live_publisher.PUBLICATION_ATTEMPTS
        insert(live, "user-late", "LATE ")
        late = await lane.submit(operation(4))
    finally:
        await close(lane, publisher, registration)
    assert all(earlier.outcome == "landed" for earlier in results)
    assert peak == 3 and owed_count(live) == 3
    for refused in (result, late):
        assert refused.outcome == "refused"
        assert refused.refusal.kind == "publication_failed"
        assert str(refused.refusal) == "registration permanently refused"
    # Every acknowledged edit, including one sent while refused, stays durable in live.
    await live.commit_received()
    assert {"user-0", "user-2", "user-late"} <= set(acked)
    from doceditor.live_document import LiveDocument
    reopened = await LiveDocument.open(live.path, live.store.path)
    assert "LATE " in reopened.text and "U2 " in reopened.text


@pytest.mark.asyncio
async def test_publisher_turning_blocked_mid_wait_wakes_and_refuses_the_waiter(tmp_path, monkeypatch):
    from doceditor import live_publisher
    registration = Registration(fail="registration refused", stall=True)
    live, lane, publisher, gate = await gated(tmp_path, monkeypatch, registration, bound=1, wait=30.0)
    try:
        assert (await lane.submit(operation(1))).outcome == "landed"
        for _ in range(live_publisher.PUBLICATION_ATTEMPTS - 1):
            live.store.record_failure("publication_failed", "registration refused")
        waiting = asyncio.create_task(lane.submit(operation(3)))
        await until(lambda: gate.waiting == 1)
        started = asyncio.get_running_loop().time()
        registration.open.set()
        result = await asyncio.wait_for(waiting, 3)
        assert asyncio.get_running_loop().time() - started < 3
    finally:
        await close(lane, publisher, registration)
    assert result.outcome == "refused" and result.refusal.kind == "publication_failed"
    assert str(result.refusal) == "registration refused"
    assert live.text == live.path.read_text() == "agent 1"


@pytest.mark.asyncio
async def test_drain_stalled_without_blocking_refuses_at_the_wait_bound(tmp_path, monkeypatch):
    from doceditor.live_backpressure import SLOW_DRAIN_MESSAGE
    registration = Registration(stall=True)
    live, lane, publisher, gate = await gated(tmp_path, monkeypatch, registration, bound=1, wait=0.3)
    try:
        assert (await lane.submit(operation(1))).outcome == "landed"
        started = asyncio.get_running_loop().time()
        result = await asyncio.wait_for(lane.submit(operation(3)), 3)
        elapsed = asyncio.get_running_loop().time() - started
    finally:
        await close(lane, publisher, registration)
    assert 0.3 <= elapsed < 2
    assert result.outcome == "refused" and result.refusal.kind == "publication_failed"
    assert str(result.refusal) == SLOW_DRAIN_MESSAGE
    assert live.store.failure("publication_failed") is None
    assert live.path.read_text() == "agent 1"


@pytest.mark.asyncio
async def test_two_admissions_at_bound_minus_one_admit_only_one(tmp_path, monkeypatch):
    registration = Registration(stall=True)
    live, lane, publisher, gate = await gated(tmp_path, monkeypatch, registration, bound=2, wait=30.0)
    try:
        assert (await lane.submit(operation(1))).outcome == "landed"
        assert owed_count(live) == 1
        first = asyncio.create_task(lane.submit(operation(3)))
        second = asyncio.create_task(lane.submit(operation(5)))
        await until(lambda: gate.waiting == 1)
        assert first.done() and (await first).outcome == "landed"
        assert not second.done() and owed_count(live) == 2
        assert live.path.read_text() == "agent 3"
        registration.open.set()
        assert (await asyncio.wait_for(second, 3)).outcome == "landed"
    finally:
        await close(lane, publisher, registration)
    assert live.path.read_text() == "agent 5"


@pytest.mark.asyncio
async def test_drain_wakes_a_waiter_without_polling(tmp_path, monkeypatch):
    registration = Registration(stall=True)
    live, lane, publisher, gate = await gated(tmp_path, monkeypatch, registration, bound=1, wait=30.0)
    counts = []
    real = live.store.owed_count
    def counted():
        counts.append(1)
        return real()
    monkeypatch.setattr(live.store, "owed_count", counted)
    try:
        assert (await lane.submit(operation(1))).outcome == "landed"
        waiting = asyncio.create_task(lane.submit(operation(3)))
        await until(lambda: gate.waiting == 1)
        before = len(counts)
        await asyncio.sleep(0.3)
        assert len(counts) == before
        registration.open.set()
        assert (await asyncio.wait_for(waiting, 3)).outcome == "landed"
    finally:
        await close(lane, publisher, registration)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel_event", "task_cancel"])
async def test_a_waiting_write_is_queued_not_started(tmp_path, monkeypatch, stop):
    from doceditor.live_lane import Operation
    registration = Registration(stall=True)
    live, lane, publisher, gate = await gated(tmp_path, monkeypatch, registration, bound=1, wait=30.0)
    try:
        assert (await lane.submit(operation(1))).outcome == "landed"
        updates = live.store.load_updates()
        cancel = asyncio.Event()
        waiting = asyncio.create_task(lane.submit(
            Operation("replace", content="never", actor=AGENT),
            cancel_event=cancel if stop == "cancel_event" else None))
        await until(lambda: gate.waiting == 1)
        assert not live.committer_lock.locked()
        before = asyncio.get_running_loop().time()
        if stop == "cancel_event":
            cancel.set()
            result = await asyncio.wait_for(waiting, 1)
            assert result.outcome == "not_started"
        else:
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
        assert asyncio.get_running_loop().time() - before < 1
        await until(lambda: gate.waiting == 0)
        registration.open.set()
        later = await asyncio.wait_for(lane.submit(Operation("flush", actor=USER)), 3)
    finally:
        await close(lane, publisher, registration)
    assert later.outcome == "landed"
    assert "never" not in live.path.read_text()
    assert live.store.load_updates()[:len(updates)] == updates


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel_event", "task_cancel"])
async def test_stop_during_admission_count_never_starts_write(tmp_path, monkeypatch, stop):
    registration = Registration()
    live, lane, publisher, gate = await gated(tmp_path, monkeypatch, registration)
    entered, release = threading.Event(), threading.Event()
    real = live.store.owed_count
    def stalled_count():
        entered.set()
        assert release.wait(3)
        return real()
    monkeypatch.setattr(live.store, "owed_count", stalled_count)
    cancel = asyncio.Event()
    waiting = asyncio.create_task(lane.submit(operation(1), cancel_event=cancel))
    try:
        await until(entered.is_set)
        if stop == "cancel_event":
            cancel.set()
            assert (await waiting).outcome == "not_started"
        else:
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
        release.set()
        await lane._worker
        assert live.path.read_text() == "start"
    finally:
        release.set()
        await close(lane, publisher, registration)


@pytest.mark.asyncio
async def test_healthy_publisher_never_refuses_alternating_writes(tmp_path, monkeypatch):
    registration = Registration()
    live, lane, publisher, gate = await gated(tmp_path, monkeypatch, registration, bound=None)
    peak = 0
    real = live.store.owed_count
    def counted():
        nonlocal peak
        value = real()
        peak = max(peak, value)
        return value
    monkeypatch.setattr(live.store, "owed_count", counted)
    try:
        for index in range(40):
            if index % 2 == 0:
                insert(live, f"user-{index}", "u")
        results = await asyncio.gather(*(lane.submit(operation(index)) for index in range(40)))
        # Lane completion owes registration; shutdown deliberately stops new rows.
        await until(lambda: live.store.owed_count() == 0)
    finally:
        await close(lane, publisher, registration)
    from doceditor.live_backpressure import OWED_ROW_BOUND
    assert all(result.outcome == "landed" for result in results)
    assert 1 <= peak < OWED_ROW_BOUND
    assert registration.published.count("agent-thread") == 20
