"""A landed write clears only the store failure it disproves."""
import asyncio

import pytest

from doceditor.live_lane import Operation
from doceditor.live_failure_record import FailureRecord
from doceditor.live_store import LiveStore, StoreRefused
from tests.test_document_live_lane import insert, open_lane, cut_at


def watch(live) -> list:
    events = []
    live.failure_changed = lambda kind, message, provenance: events.append((kind, message))
    return events


async def landed(lane, text):
    result = await lane.submit(Operation("replace", content=text))
    assert result.server_saved
    return result


async def close(live, lane):
    await lane.close()
    live.close()


@pytest.mark.asyncio
async def test_landed_write_clears_an_older_store_failure(tmp_path):
    live, lane = await open_lane(tmp_path)
    events = watch(live)
    live.store.record_failure("store_refused", "disk full")
    try:
        await landed(lane, "landed")
        assert live.store.failure("store_refused") is None
        assert events == [("store_refused", "")]
    finally:
        await close(live, lane)


@pytest.mark.asyncio
async def test_failure_recorded_during_an_in_flight_write_survives_it(tmp_path, monkeypatch):
    async def cut(stage):
        if stage == "rename":
            await asyncio.to_thread(live.store.record_failure, "store_refused", "later failure")
    cut_at(monkeypatch, cut)
    live, lane = await open_lane(tmp_path)
    events = watch(live)
    try:
        await landed(lane, "landed")
        assert live.store.failure("store_refused") == FailureRecord(1, "later failure", 1)
        assert events == []
    finally:
        await close(live, lane)


@pytest.mark.asyncio
async def test_landed_write_never_clears_setup_or_publication_failures(tmp_path):
    live, lane = await open_lane(tmp_path)
    live.store.record_failure("setup_failed", "setup refused")
    live.store.record_failure("publication_failed", "publication refused")
    try:
        await landed(lane, "landed")
        assert live.store.failure("setup_failed") == FailureRecord(1, "setup refused", 1)
        assert live.store.failure("publication_failed") == FailureRecord(1, "publication refused", 2)
    finally:
        await close(live, lane)


@pytest.mark.asyncio
async def test_browser_commit_clears_nothing(tmp_path):
    live, lane = await open_lane(tmp_path)
    events = watch(live)
    live.store.record_failure("store_refused", "disk full")
    try:
        insert(live, "typed")
        assert await live.commit_received() == ("typed",)
        assert live.store.failure("store_refused") == FailureRecord(1, "disk full", 1)
        assert events == []
    finally:
        await close(live, lane)


@pytest.mark.asyncio
async def test_generation_holds_across_restart(tmp_path, monkeypatch):
    async def cut(stage):
        if stage == "rename":
            # Another process instance records it while this write is in flight.
            await asyncio.to_thread(LiveStore(live.store.path).record_failure, "store_refused", "after start")
    cut_at(monkeypatch, cut)
    live, lane = await open_lane(tmp_path)
    try:
        await landed(lane, "before restart")
        assert live.store.failure("store_refused") == FailureRecord(1, "after start", 1)
    finally:
        await close(live, lane)
    cut_at(monkeypatch, None)
    live, lane = await open_lane(tmp_path)
    try:
        assert live.store.failure("store_refused") == FailureRecord(1, "after start", 1)
        await landed(lane, "after restart")
        assert live.store.failure("store_refused") is None
    finally:
        await close(live, lane)


@pytest.mark.asyncio
async def test_refused_failure_record_leaves_nothing_durable(tmp_path, monkeypatch):
    live, lane = await open_lane(tmp_path)
    events = watch(live)
    def refused(*args, **kwargs):
        raise StoreRefused("store gone")
    monkeypatch.setattr(live.store, "record_failure", refused)
    try:
        await lane.retry.record_failure(StoreRefused("disk full"))
        assert events and events[-1][0] == "store_refused"
        assert live.store.failure("store_refused") is None
        assert live.store.failure_generation() == 0
    finally:
        await close(live, lane)


@pytest.mark.asyncio
async def test_only_the_writer_started_after_the_failure_clears_it(tmp_path, monkeypatch):
    first_started = asyncio.Event()
    async def cut(stage):
        if stage == "rename" and not first_started.is_set():
            first_started.set()
            await asyncio.to_thread(live.store.record_failure, "store_refused", "mid write")
    cut_at(monkeypatch, cut)
    live, lane = await open_lane(tmp_path)
    events = watch(live)
    try:
        # Both are queued before the failure; the second starts after it.
        first = asyncio.create_task(lane.submit(Operation("replace", content="first")))
        second = asyncio.create_task(lane.submit(Operation("replace", content="second")))
        assert (await first).server_saved
        assert live.store.failure("store_refused") == FailureRecord(1, "mid write", 1)
        assert (await second).server_saved
        assert live.store.failure("store_refused") is None
        assert events == [("store_refused", "")]
    finally:
        await close(live, lane)


def test_store_failure_generation_is_durable_and_monotonic(tmp_path):
    store = LiveStore(tmp_path / "state.sqlite")
    assert store.failure_generation() == 0
    store.record_failure("store_refused", "one")
    store.record_failure("setup_failed", "two")
    store.clear_failure("store_refused")
    assert LiveStore(store.path).failure_generation() == 2
