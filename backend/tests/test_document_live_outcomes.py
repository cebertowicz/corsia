"""Typed lane outcomes and independent ownership after real cancellation."""
import asyncio

import pytest

from doceditor.live_lane import LaneRefused, Operation
from doceditor.live_publication_record import WriteActor
from tests.test_document_live_lane import capture_jobs, open_lane, settled_refusal, cut_at


@pytest.mark.asyncio
async def test_real_cancel_propagates_without_waiting_for_started_write(tmp_path, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    async def cut(stage):
        if stage == "record":
            entered.set()
            await release.wait()
    cut_at(monkeypatch, cut)
    live, lane = await open_lane(tmp_path)
    call = asyncio.create_task(lane.submit(Operation("replace", content="late")))
    try:
        await entered.wait()
        call.cancel("actual cancellation")
        done, _ = await asyncio.wait({call}, timeout=.05)
        assert call in done, "task cancellation must not wait for the lane"
        with pytest.raises(asyncio.CancelledError, match="actual cancellation"):
            await call
        assert not lane._worker.done()
        release.set()
        await lane._worker
        assert live.path.read_text() == live.text == "late"
    finally:
        release.set()
        await asyncio.gather(call, return_exceptions=True)
        await lane.close()
        live.close()


@pytest.mark.asyncio
async def test_submit_returns_typed_landed_and_refused_outcomes(tmp_path):
    live, lane = await open_lane(tmp_path)
    try:
        landed = await lane.submit(Operation("replace", content="landed"))
        assert landed.outcome == "landed" and landed.server_saved
        refused = await lane.submit(Operation("write", content="unsafe", base_hash="unknown"))
        assert refused.outcome == "refused"
        assert isinstance(refused.refusal, LaneRefused)
        assert live.path.read_text() == "landed"
    finally:
        await lane.close()
        live.close()


@pytest.mark.asyncio
async def test_native_event_returns_not_started_or_pending_values(tmp_path, monkeypatch):
    from doceditor import live_lane
    monkeypatch.setattr(live_lane, "NATIVE_SETTLE_TIMEOUT_S", .02, raising=False)
    entered, release = asyncio.Event(), asyncio.Event()
    async def cut(stage):
        if stage == "record":
            entered.set()
            await release.wait()
    cut_at(monkeypatch, cut)
    live, lane = await open_lane(tmp_path)
    first_event, queued_event = asyncio.Event(), asyncio.Event()
    call = asyncio.create_task(lane.submit(Operation("replace", content="late"), cancel_event=first_event))
    try:
        await entered.wait()
        queued_event.set()
        queued = await lane.submit(Operation("replace", content="never"), cancel_event=queued_event)
        assert queued.outcome == "not_started"
        first_event.set()
        pending = await call
        assert pending.outcome == "pending"
        assert asyncio.current_task().cancelling() == 0
        release.set()
        await lane._worker
        assert live.text == live.path.read_text() == "late"
    finally:
        release.set()
        await asyncio.gather(call, return_exceptions=True)
        await lane.close()
        live.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal_kind", ["lane", "edit", "policy", "setup", "store"])
async def test_detached_refusal_settles_its_job_only_store_errors_spend_budget(tmp_path, monkeypatch, refusal_kind):
    from doceditor.live_lane import EditRefused
    from doceditor.document_save import DocumentSaveError
    from registry.shared_write_errors import SharedIdentitySetupRequired
    from doceditor.live_store import StoreRefused
    failures = {"lane": LaneRefused("lane refused"), "edit": EditRefused("Error: unmatched"),
                "policy": DocumentSaveError("policy", "policy refused"),
                "setup": SharedIdentitySetupRequired(), "store": StoreRefused("disk full")}
    entered, release = asyncio.Event(), asyncio.Event()
    live, lane = await open_lane(tmp_path)
    async def validate(text, actor):
        entered.set()
        await release.wait()
        raise failures[refusal_kind]
    lane.validate_actor = validate
    jobs = capture_jobs(lane, monkeypatch)
    events = []
    live.failure_changed = lambda kind, message, provenance: events.append((kind, message))
    call = asyncio.create_task(lane.submit(Operation("replace", content="refused", actor=WriteActor("author", None))))
    try:
        await entered.wait()
        call.cancel()
        done, _ = await asyncio.wait({call}, timeout=.05)
        assert call in done
        release.set()
        await lane._worker
        assert settled_refusal(jobs[0]) is failures[refusal_kind]
        assert live.path.read_text() == "hello quote"
        # Only a store failure spends the budget, and one failure does not block.
        assert not lane.retry.blocked_reason and not events
        from doceditor.live_failure_record import FailureRecord
        assert live.store.failure("store_refused") == (FailureRecord(1, "disk full", 1) if refusal_kind == "store" else None)
    finally:
        release.set()
        await asyncio.gather(call, return_exceptions=True)
        await lane.close()
        live.close()
