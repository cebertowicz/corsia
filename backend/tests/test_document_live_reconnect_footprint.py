"""Measured journal cost of normal reconnect diffs after lost durable acks."""
import sqlite3

import pytest

from doceditor.live_document import LiveDocument
from doceditor.live_store import BrowserMessage
from doceditor.live_text import clone


def journal_bytes(store):
    with sqlite3.connect(store.path) as db:
        return db.execute("SELECT COALESCE(SUM(length(upd)),0), COUNT(*) FROM live").fetchone()


@pytest.mark.asyncio
async def test_twenty_lost_ack_reconnects_on_fifty_kib_document(tmp_path):
    path, state = tmp_path / "doc.md", tmp_path / "live.sqlite"
    path.write_text("x" * (50 * 1024))
    live = await LiveDocument.open(path, state)
    client = clone(live.doc.get_update())
    before_journal, _ = journal_bytes(live.store)
    before_file = state.stat().st_size
    # Deleting an item adds no struct to the vector. Every close-sync still
    # carries the complete deletion set, even after the server stored it.
    del client["text"][0:1]
    for _ in range(20):
        message = BrowserMessage("lost-ack-delete", client.get_update(live.doc.get_state()))
        assert live.receive(message)
        assert await live.commit_received() == (message.message_id,)
        live.close()
        live = await LiveDocument.open(path, state)
        assert live.text == "x" * (50 * 1024 - 1)
    after_journal, rows = journal_bytes(live.store)
    after_file = state.stat().st_size
    document_bytes = len(live.text.encode("utf-8"))
    print(f"reconnects=20;document-bytes={document_bytes};journal-before={before_journal};"
          f"journal-after={after_journal};journal-growth={after_journal-before_journal};"
          f"journal-document-ratio={after_journal/document_bytes:.4f};rows={rows};"
          f"sqlite-before={before_file};sqlite-after={after_file}")
    live.close()
