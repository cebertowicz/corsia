"""Author refusals are named types; everything else is a storage or code failure."""
import asyncio

import pytest

from doceditor.comment_action_errors import CommentActionError
from doceditor.document_save import DocumentSaveError
from doceditor.live_lane import EditRefused, LaneRefused, Operation, is_ordinary_refusal
from doceditor.live_store import StoreRefused
from registry.shared_write_errors import DurableWritePolicyError, SharedIdentitySetupRequired
from tests.test_document_live_lane import capture_jobs, open_lane


def _decode_error() -> UnicodeDecodeError:
    try:
        b"\xff".decode("utf-8")
    except UnicodeDecodeError as error:
        return error
    raise AssertionError("expected a decode error")


@pytest.mark.parametrize(("error", "ordinary"), [
    (LaneRefused("stale"), True),
    (EditRefused("Error: unmatched"), True),
    (CommentActionError("stale", "suggestion changed"), True),
    (DocumentSaveError("too_large", "too large"), True),
    (DurableWritePolicyError("policy refused"), True),
    (SharedIdentitySetupRequired(), True),
    (ValueError("a bug"), False),
    (_decode_error(), False),
    (PermissionError("disk permission"), False),
    (StoreRefused("disk full"), False),
])
def test_ordinary_refusals_are_an_explicit_set(error, ordinary):
    assert is_ordinary_refusal(error) is ordinary


@pytest.mark.asyncio
async def test_undecodable_disk_bytes_fail_an_attached_write_as_storage(tmp_path):
    live, lane = await open_lane(tmp_path)
    live.path.write_bytes(b"\xff\xfe not text")
    try:
        with pytest.raises(OSError, match="UTF-8"):
            await lane.submit(Operation("replace", content="changed"))
        assert live.path.read_bytes() == b"\xff\xfe not text"
    finally:
        await lane.close()
        live.close()


@pytest.mark.asyncio
async def test_undecodable_disk_bytes_spend_a_detached_writes_budget(tmp_path, monkeypatch):
    live, lane = await open_lane(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    barrier = lane._barrier
    async def held():
        entered.set()
        await release.wait()
        await barrier()
    monkeypatch.setattr(lane, "_barrier", held)
    jobs = capture_jobs(lane, monkeypatch)
    call = asyncio.create_task(lane.submit(Operation("replace", content="changed")))
    try:
        await entered.wait()
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        live.path.write_bytes(b"\xff\xfe not text")
        release.set()
        await lane._worker
        assert isinstance(jobs[0].future.exception(), OSError)
        failure = live.store.failure("store_refused")
        assert failure is not None and failure.attempts == 1 and failure.generation == 1 and "UTF-8" in failure.message
    finally:
        release.set()
        await lane.close()
        live.close()
