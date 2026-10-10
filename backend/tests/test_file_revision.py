"""``file_revision`` is ``revision(bytes)`` computed one chunk at a time."""

from __future__ import annotations

import tracemalloc

import pytest

from doceditor.document_scope import REVISION_CHUNK_BYTES, file_revision, revision


@pytest.mark.parametrize("size", [0, 1, 7, 8, 9, 1000])
def test_matches_the_whole_file_revision_across_chunk_boundaries(tmp_path, size):
    data = bytes(range(256)) * (size // 256 + 1)
    path = tmp_path / "f.bin"
    path.write_bytes(data[:size])
    assert file_revision(path, chunk_bytes=8) == revision(data[:size])


def test_default_chunk_is_one_megabyte_and_matches_a_multi_chunk_file(tmp_path):
    assert REVISION_CHUNK_BYTES == 1024 * 1024
    data = b"\x89PNG" + bytes(REVISION_CHUNK_BYTES * 2 + 12345)
    path = tmp_path / "big.png"
    path.write_bytes(data)
    assert file_revision(path) == revision(data)


def test_memory_stays_at_a_chunk_not_the_file(tmp_path):
    size = 12 * REVISION_CHUNK_BYTES
    path = tmp_path / "huge.png"
    path.write_bytes(bytes(size))
    tracemalloc.start()
    try:
        file_revision(path)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # One 1 MB chunk plus interpreter overhead; reading the whole file would peak at 12 MB.
    assert peak < 3 * REVISION_CHUNK_BYTES
