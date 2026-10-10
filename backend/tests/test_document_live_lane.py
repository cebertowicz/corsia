"""Real store/lane ordering and crash-recovery boundaries."""
import asyncio
from dataclasses import replace

import pytest

from doceditor.live_document import LiveDocument, file_hash
from doceditor.live_store import BrowserMessage, StoreRefused
from doceditor.live_text import clone


async def open_lane(tmp_path, *, validate=None, write=None):
    from doceditor.live_lane import DocumentLane
    from doceditor.document_save import write_atomic_plain

    path = tmp_path / "document.md"
    if not path.exists():
        path.write_text("hello quote")
    live = await LiveDocument.open(path, tmp_path / "state.sqlite")

    async def valid(text, actor):
        if validate:
            validate(text)

    async def writer(text, prior_hash):
        if write is not None:
            await asyncio.to_thread(write, path, text)
        else:
            await asyncio.to_thread(write_atomic_plain, path, text, prior_hash)

    lane = DocumentLane(live, validate_actor=valid, write=writer)
    return live, lane


async def _no_cut(stage):
    return None


def cut_at(monkeypatch, hook):
    """Install ``hook`` at the lane's write stages for this test only."""
    from doceditor import live_lane
    monkeypatch.setattr(live_lane, "_checkpoint", hook or _no_cut)


def capture_jobs(lane, monkeypatch) -> list:
    """Record each queued job so a test can read what a detached caller's job settled with."""
    jobs, enqueue = [], lane._enqueue
    def recording(*args, **kwargs):
        jobs.append(enqueue(*args, **kwargs))
        return jobs[-1]
    monkeypatch.setattr(lane, "_enqueue", recording)
    return jobs


def settled_refusal(job):
    """The refusal a job settled with, whether the lane returned it or raised it."""
    return job.future.exception() or job.future.result().refusal


def insert(live, identifier, text="USER "):
    peer = clone(live.doc.get_update())
    state = peer.get_state()
    peer["text"].insert(0, text)
    live.receive(BrowserMessage(identifier, peer.get_update(state)))


@pytest.mark.asyncio
async def test_write_based_on_read_preserves_live_typing(tmp_path):
    from doceditor.live_lane import Operation

    live, lane = await open_lane(tmp_path)
    base = file_hash(live.path.read_bytes())
    insert(live, "typing")
    result = await lane.submit(Operation("write", content="hello world", base_hash=base))
    assert live.text == live.path.read_text() == "USER hello world"
    assert result.merged and result.server_saved
    assert (await LiveDocument.open(live.path, live.store.path)).text == live.text


@pytest.mark.asyncio
async def test_two_writes_same_read_and_flush_in_fifo_order(tmp_path):
    from doceditor.live_lane import Operation

    live, lane = await open_lane(tmp_path)
    base = file_hash(live.path.read_bytes())
    insert(live, "typing")
    await lane.submit(Operation("write", content="FIRST hello quote", base_hash=base))
    await lane.submit(Operation("flush"))
    await lane.submit(Operation("write", content="hello quote SECOND", base_hash=base))
    assert live.path.read_text() == live.text
    assert live.text.count("FIRST") == live.text.count("SECOND") == live.text.count("USER") == 1


@pytest.mark.asyncio
async def test_unknown_base_refused_after_import_without_overwriting(tmp_path):
    from doceditor.live_lane import Operation, LaneRefused

    live, lane = await open_lane(tmp_path)
    live.path.write_text("OUTSIDE hello quote")
    with pytest.raises(LaneRefused, match="This document changed since you read it; read it again, then write"):
        refused = await lane.submit(Operation("write", content="wrong", base_hash="unknown"))
        assert refused.outcome == "refused"
        refused.require_landed()
    assert live.path.read_text() == live.text == "OUTSIDE hello quote"


@pytest.mark.asyncio
async def test_durable_closure_ack_precedes_rename_and_late_update_is_next_batch(tmp_path, monkeypatch):
    from doceditor.live_lane import Operation

    entered = asyncio.Event()
    release = asyncio.Event()

    async def cut(stage):
        if stage == "record":
            entered.set()
            await release.wait()

    cut_at(monkeypatch, cut)
    live, lane = await open_lane(tmp_path)
    acks = []
    live.acknowledge = acks.append
    insert(live, "before")
    task = asyncio.create_task(lane.submit(Operation("flush")))
    await entered.wait()
    assert acks == [("before",)]
    insert(live, "during", "LATE ")
    assert await live.commit_received() == ("during",)
    release.set()
    await task
    assert live.path.read_text() == "USER hello quote"
    assert live.text == "LATE USER hello quote"
    await lane.submit(Operation("flush"))
    assert live.path.read_text() == live.text
    assert acks == [("before",), ("during",)]


@pytest.mark.asyncio
async def test_refused_promote_barrier_then_recovery(tmp_path):
    from doceditor.live_lane import Operation, LaneRefused

    live, lane = await open_lane(tmp_path)
    original = live.store.promote

    def refused(*_):
        raise StoreRefused("refused promote")

    live.store.promote = refused
    result = await lane.submit(Operation("replace", content="NEW"))
    assert not result.server_saved
    assert live.path.read_text() == live.text == "NEW"
    live.path.write_text("OUTSIDE")
    with pytest.raises(LaneRefused):
        refused = await lane.submit(Operation("flush"))
        assert refused.outcome == "refused"
        refused.require_landed()
    assert live.text == "NEW"
    live.path.write_text("NEW")
    live.store.promote = original
    await lane.recover()
    assert live.store.pending() is None
    await lane.submit(Operation("flush"))


@pytest.mark.asyncio
async def test_refused_rename_discards_pending_and_keeps_typing(tmp_path):
    from doceditor.live_lane import Operation

    def refused(path, text):
        raise PermissionError("rename refused")

    live, lane = await open_lane(tmp_path, write=refused)
    insert(live, "typing")
    with pytest.raises(PermissionError):
        await lane.submit(Operation("replace", content="NEW"))
    assert live.store.pending() is None
    assert live.text == "USER hello quote"
    assert live.path.read_text() == "hello quote"


@pytest.mark.asyncio
async def test_validator_refusal_before_record(tmp_path):
    from doceditor.live_lane import Operation

    def refused(text):
        raise ValueError("invalid document")

    live, lane = await open_lane(tmp_path, validate=refused)
    with pytest.raises(ValueError):
        await lane.submit(Operation("replace", content="invalid"))
    assert live.store.pending() is None
    assert live.text == live.path.read_text() == "hello quote"


@pytest.mark.asyncio
async def test_queued_cancel_drops_write_started_cancel_leaves_actual_result_to_lane(tmp_path, monkeypatch):
    from doceditor.live_lane import Operation

    entered = asyncio.Event()
    release = asyncio.Event()

    async def cut(stage):
        if stage == "record":
            entered.set()
            await release.wait()

    cut_at(monkeypatch, cut)
    live, lane = await open_lane(tmp_path)
    first = asyncio.create_task(lane.submit(Operation("replace", content="FIRST")))
    await entered.wait()
    second = asyncio.create_task(lane.submit(Operation("replace", content="SECOND")))
    await asyncio.sleep(0)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(first), .1)
    assert not lane._worker.done()
    release.set()
    await lane._worker
    assert live.text == live.path.read_text() == "FIRST"
    await lane.close()
    live.close()


@pytest.mark.asyncio
async def test_native_cancel_event_checked_before_queue_start(tmp_path):
    from doceditor.live_lane import Operation

    live, lane = await open_lane(tmp_path)
    cancel = asyncio.Event()
    cancel.set()
    result = await lane.submit(Operation("replace", content="wrong"), cancel_event=cancel)
    assert result.outcome == "not_started"
    assert live.text == live.path.read_text() == "hello quote"
    await lane.close()
    live.close()
