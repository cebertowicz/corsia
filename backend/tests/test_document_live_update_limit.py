"""Browser updates cannot grow live text beyond the editor's document limit."""
import pytest

from doceditor.document_scope import MAX_TEXT_BYTES
from doceditor.live_document import LiveDocument
from doceditor.live_store import BrowserMessage
from doceditor.live_text import clone


@pytest.mark.asyncio
async def test_oversized_live_text_is_refused_before_merge(tmp_path):
    path = tmp_path / "large.md"
    path.write_text("a" * MAX_TEXT_BYTES)
    live = await LiveDocument.open(path, tmp_path / "store.sqlite")
    try:
        client = clone(live.doc.get_update())
        prior = client.get_state()
        client["text"].insert(MAX_TEXT_BYTES, "x")
        message = BrowserMessage("too-large", client.get_update(prior))
        with pytest.raises(ValueError, match="too large"):
            live.receive(message)
        assert live.text == "a" * MAX_TEXT_BYTES
        assert live.received == {} and live.backlog_bytes == 0
    finally:
        live.close()
