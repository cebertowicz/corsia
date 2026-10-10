"""Map all tracked UTF-16 edges from one UTF-8 buffer per real update."""
import pytest

from doceditor.live_text import clone, utf16_length
from tests.test_document_live_lane import open_lane


class EncodedText(str):
    calls = 0

    def encode(self, *args, **kwargs):
        self.calls += 1
        return super().encode(*args, **kwargs)


@pytest.mark.asyncio
async def test_many_unicode_ranges_encode_before_text_once(tmp_path):
    before = "é🙂a中" * 200
    (tmp_path / "document.md").write_text(before)
    live, lane = await open_lane(tmp_path)
    try:
        for index in range(200):
            start = index * 5
            live.track_range(f"word-{index}", start, start + 5)
            live.track_range(f"point-{index}", start, start)
        counted = EncodedText(live.text)
        live._before = counted
        other = clone(live.doc.get_update())
        with other.transaction():
            # Delete the first emoji then insert another multi-byte prefix.
            del other["text"][2:6]
            other["text"].insert(0, "中🙃")
        live.doc.apply_update(other.get_update(live.doc.get_state()))
        assert counted.calls == 1
        assert live.text == "中🙃éa中" + before[4:]
        assert live.ranges["word-0"] == (3, 6)
        assert live.ranges["point-0"] == (0, 0)
        for index in range(1, 200):
            assert live.ranges[f"word-{index}"] == (index * 5 + 1, index * 5 + 6)
            assert live.ranges[f"point-{index}"] == (index * 5 + 1, index * 5 + 1)
        assert live.snapshot == (live.text, live.ranges)
        assert utf16_length(live.text) == 1001
    finally:
        await lane.close()
        live.close()
