"""An outside write that lands after the import check is never overwritten.

The lane compares the file with the write's prior hash in the same
synchronous step as the rename. On a mismatch nothing is replaced, the
pending row is discarded, and the write re-runs on top of the imported text.
"""
import pytest

from doceditor.live_document import file_hash
from doceditor.live_lane import Operation, TextEdit, is_ordinary_refusal
from tests.test_document_live_lane import cut_at, open_lane


def outside_writer(live, texts):
    """Write the next outside text each time the lane has recorded its pending row."""
    remaining = list(texts)

    async def cut(stage):
        if stage == "record" and remaining:
            live.path.write_text(remaining.pop(0))
    return cut


async def settled(live, lane):
    assert live.store.pending() is None
    assert live.store.mirror().file_hash == file_hash(live.path.read_bytes())
    assert live.text == live.path.read_text()


@pytest.mark.asyncio
async def test_outside_write_after_record_is_imported_and_the_write_lands_on_top(tmp_path, monkeypatch):
    live, lane = await open_lane(tmp_path)
    cut_at(monkeypatch, outside_writer(live, ["OUTSIDE hello quote"]))
    try:
        result = await lane.submit(Operation("edit", edits=(TextEdit("quote", "QUOTE"),)))
        assert result.server_saved and result.refusal is None
        assert live.path.read_text() == "OUTSIDE hello QUOTE"
        await settled(live, lane)
    finally:
        await lane.close()
        live.close()


@pytest.mark.asyncio
async def test_outside_write_on_every_attempt_is_kept_and_the_write_refused(tmp_path, monkeypatch):
    live, lane = await open_lane(tmp_path)
    cut_at(monkeypatch, outside_writer(live, [f"outside {index} quote" for index in range(10)]))
    try:
        result = await lane.submit(Operation("edit", edits=(TextEdit("quote", "QUOTE"),)))
        assert not result.server_saved and is_ordinary_refusal(result.refusal)
        assert live.path.read_text().startswith("outside ") and "QUOTE" not in live.path.read_text()
        cut_at(monkeypatch, None)
        assert (await lane.submit(Operation("flush"))).server_saved
        await settled(live, lane)
    finally:
        await lane.close()
        live.close()
