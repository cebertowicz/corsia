"""Every merge into the live document refreshes its snapshot, tracked ranges or not."""
import pytest

from doceditor.live_document import file_hash
from doceditor.live_store import BrowserMessage
from doceditor.live_text import apply_text, clone
from tests.test_document_live_lane import open_lane


def edit(live, text):
    other = clone(live.doc.get_update())
    apply_text(other, text)
    return other.get_update(live.doc.get_state())


@pytest.mark.asyncio
async def test_every_merge_path_refreshes_the_snapshot(tmp_path):
    live, lane = await open_lane(tmp_path)
    changes = []
    live.text_changed = lambda: changes.append(live.snapshot[0])
    try:
        assert live.receive(BrowserMessage("browser", edit(live, "hello browser quote")))
        assert live.snapshot == ("hello browser quote", {})
        await live.commit_received()
        committed = "hello committed quote"
        await live.apply_committed(edit(live, committed), live.doc.get_update(), file_hash(committed.encode()))
        assert live.snapshot == (committed, {})
        live.apply_unsaved(edit(live, "hello unsaved quote"))
        assert live.snapshot == ("hello unsaved quote", {})
        assert changes == ["hello browser quote", committed, "hello unsaved quote"]
        live.track_range("quote", 14, 19)
        live.apply_unsaved(edit(live, "well, hello unsaved quote"))
        assert live.snapshot == ("well, hello unsaved quote", {"quote": (20, 25)})
        assert live.range_text("quote") == "quote"
    finally:
        await lane.close()
        live.close()
