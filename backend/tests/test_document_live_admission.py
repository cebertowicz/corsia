"""One rule decides whether a document identity may open live at a path."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from doceditor.live_admission import open_refusal
from doceditor.live_runtime import DocumentLifetime


def _entry(path: str):
    return SimpleNamespace(live=SimpleNamespace(path=Path(path)))


@pytest.mark.asyncio
@pytest.mark.parametrize(("open_now", "artifact_id", "path", "cleaning", "expected"), [
    ({}, "a", "/doc.md", True, "cleanup_running"),
    ({"a": "/old.md"}, "a", "/doc.md", False, "document_moved"),
    ({"b": "/doc.md"}, "a", "/doc.md", False, "path_open_elsewhere"),
    ({"a": "/doc.md"}, "a", "/doc.md", False, None),
    ({"b": "/other.md"}, "a", "/doc.md", False, None),
])
async def test_open_refusal_table(open_now, artifact_id, path, cleaning, expected):
    owner = DocumentLifetime(asyncio.Lock())
    if cleaning:
        owner.closing = asyncio.get_running_loop().create_future()
    documents = {key: _entry(value) for key, value in open_now.items()}
    refused = open_refusal(documents, artifact_id, Path(path), owner)
    assert (refused and refused.kind) == expected
