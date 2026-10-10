"""The store keeps only the latest disk mirror."""
import sqlite3

import pytest

from doceditor.live_document import LiveDocument, file_hash
from doceditor.live_lane import Operation
from tests.test_document_live_lane import open_lane


def mirror_rows(live) -> int:
    with sqlite3.connect(live.store.path) as db:
        return db.execute("SELECT COUNT(*) FROM mirror").fetchone()[0]


@pytest.mark.asyncio
async def test_writes_and_imports_keep_one_mirror_row(tmp_path):
    live, lane = await open_lane(tmp_path)
    try:
        for index in range(5):
            assert (await lane.submit(Operation("replace", content=f"write {index}"))).server_saved
        live.path.write_text("changed outside")
        assert (await lane.submit(Operation("flush"))).server_saved
        assert mirror_rows(live) == 1
        assert live.store.mirror().file_hash == file_hash(live.path.read_bytes())
    finally:
        await lane.close()
        live.close()
    reopened = await LiveDocument.open(live.path, live.store.path)
    try:
        assert reopened.text == "changed outside"
    finally:
        reopened.close()
