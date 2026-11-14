"""Keep an open document's typing in its file while that file moves to another path."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from doceditor.live_lane import Operation
from doceditor.live_runtime import LiveDocuments, LiveEntry


class DocumentNotSaved(RuntimeError):
    """An open document holds typing that could not be saved to its file, so its file must not move."""


@asynccontextmanager
async def hold_for_artifact(documents: LiveDocuments, artifact_id: str) -> AsyncIterator[None]:
    """``hold_for_move`` for a caller that knows the document's identity, not its path."""
    entry = documents.documents.get(artifact_id)
    if entry is None:
        yield
        return
    async with hold_for_move(documents, entry.live.path):
        yield


@asynccontextmanager
async def hold_for_move(documents: LiveDocuments, path: Path) -> AsyncIterator[None]:
    """Save and retire the document open at ``path``, then keep it closed while its file moves.

    The path's lock stays held until the block ends, so no open or agent write that waits on it
    reaches the file in between. Typing the store has accepted is saved to the file first, so the
    moved file holds it; when that cannot be done, nothing moves. A viewer left on the retired entry
    is refused ("reopen it") and the retired entry accepts no more typing. A move that is then
    refused for another reason leaves the document closed, and the next open reads the same file.
    """
    owner = documents._lifetime(path)
    if owner.closing is not None:
        await asyncio.shield(owner.closing)
    async with owner.lock:
        entry = next((item for item in tuple(documents.documents.values()) if item.live.path == path), None)
        if entry is not None:
            async with entry.live.comment_mutex:
                await _save_open_text(entry)
                await documents.release(entry.artifact_id, entry)
            _refuse_late_typing(entry)
        yield


def _refuse_late_typing(entry: LiveEntry) -> None:
    """Typing that arrived while the entry was closing is not in the file, so the file must not move."""
    live = entry.live
    if live.received or live.text != live.on_disk[1]:
        raise DocumentNotSaved(_NOT_SAVED)


_NOT_SAVED = (
    "This document is open with changes that could not be saved yet, so it was not moved. "
    "Try again shortly."
)


async def _save_open_text(entry: LiveEntry) -> None:
    live = entry.live
    try:
        await live.commit_received()
        if live.text == live.on_disk[1]:
            return
        actor = live.viewer_actor()
        if actor is None:
            raise DocumentNotSaved("no writable viewer holds the unsaved text")
        (await entry.lane.submit(Operation("flush", actor=actor))).require_landed()
    except Exception as error:
        raise DocumentNotSaved(_NOT_SAVED) from error
