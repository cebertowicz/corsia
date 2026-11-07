"""A person's edits holding a document section's writer turn.

The editor already sends every local change as a ``doc_update`` frame (``ws/document_control``).
The section a change lands in (``doc_sections``) is held by that page, a socket on one document,
from then until the page's next change lands in another section, ``IDLE_SECONDS`` pass with no
change (the design's Idle), the page closes the document, or its socket drops. The hold is
``writer_turns``' person hold under the section's key; whenever a section frees, the first agent
waiting on it is woken. A page that only views a section holds nothing.

Holds are kept in memory only, so a restart frees them all, as it does a comment thread's.
"""

from __future__ import annotations

import asyncio

from loguru import logger

from doceditor import doc_sections, writer_turns

#: A page whose person makes no change for this long loses its section.
IDLE_SECONDS = 120

_idle: dict[tuple[object, str], asyncio.TimerHandle] = {}
_tasks: set[asyncio.Task] = set()


async def _serve(freed: list[str]) -> None:
    from doceditor.agent_comment_writes import serve_freed

    try:
        await serve_freed(freed)
    except Exception as exc:
        logger.warning(f"section_turn: serving {freed} failed: {exc}")


def _spawn(freed: list[str]) -> None:
    if not freed:
        return
    try:
        task = asyncio.get_running_loop().create_task(_serve(freed))
    except RuntimeError:
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _forget(page: tuple[object, str]) -> None:
    handle = _idle.pop(page, None)
    if handle is not None:
        handle.cancel()


def _expire(page: tuple[object, str]) -> None:
    _idle.pop(page, None)
    _spawn(writer_turns.release(page))


def note_edit(socket, user: dict, artifact_id: str, live, before: str) -> None:
    """The person's change on ``socket`` merged into ``live``; ``before`` is the text it merged into. Never raises."""
    try:
        after = live.snapshot[0]
        if after is before or after == before:
            return
        start, _, _ = doc_sections.changed_span(before, after)
        key = writer_turns.section_key(artifact_id, doc_sections.section_at(after, start))
        page = (socket, artifact_id)
        if writer_turns.page_root(page) != key:
            live.sections.changed([writer_turns.section_heading(key)])
        freed = writer_turns.take(page, key, writer_turns.PersonHold(
            user_id=user["id"], name=str(user.get("name") or "someone"), artifact_id=artifact_id))
        _forget(page)
        _idle[page] = asyncio.get_running_loop().call_later(IDLE_SECONDS, _expire, page)
        _spawn(freed)
    except Exception as exc:
        logger.warning(f"section_turn: edit on {artifact_id} not held: {exc}")


def leave(socket, artifact_id: str) -> None:
    """The page closed the document."""
    page = (socket, artifact_id)
    _forget(page)
    _spawn(writer_turns.release(page))


def disconnect(socket) -> None:
    """A closed socket's pages leave their sections at once."""
    for page in [page for page in _idle if page[0] is socket]:
        leave(socket, page[1])


def reset() -> None:
    """Forget every idle timer (tests)."""
    for handle in _idle.values():
        handle.cancel()
    _idle.clear()
