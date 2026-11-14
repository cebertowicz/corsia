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
async def hold_for_move(documents: LiveDocuments, path: Path) -> AsyncIterator[None]:
    """Save and retire the document open at ``path``, then keep it closed while its file moves.

    The path's lock is taken whether or not a document is open, and stays held until the block ends,
    so no open or agent write that waits on it reaches the file in between. Callers run every check
    that can refuse the move before entering, so a refused call closes nobody's document. Typing the store has accepted is saved to the file first, so the
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


_NOT_SAVED = "This document has changes that couldn’t be saved, so it wasn’t moved. Try again in a moment."


SAVE_ROUNDS = 5


async def _save_open_text(entry: LiveEntry) -> None:
    """Save until the file holds the live text, so typing that arrives during a save is saved too.

    Returns with no await after its last check, so the caller's retire starts before more typing can land.
    """
    live = entry.live
    try:
        for _ in range(SAVE_ROUNDS):
            await live.commit_received()
            if live.text == live.on_disk[1]:
                return
            actor = live.viewer_actor()
            if actor is None:
                raise DocumentNotSaved("no writable viewer holds the unsaved text")
            (await entry.lane.submit(Operation("flush", actor=actor))).require_landed()
        if live.text != live.on_disk[1]:
            raise DocumentNotSaved("typing kept arriving while the document was being saved")
    except Exception as error:
        raise DocumentNotSaved(_NOT_SAVED) from error
