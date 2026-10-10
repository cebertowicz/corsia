"""The document route for streamed tool input: an agent's edit, comment or suggestion, shown while it is written.

One provisional-argument contract for every harness: ``start`` names a call,
``update`` adds a fragment of its JSON input, ``end`` closes it for any reason.
The three run on a provider's event pump, so they stay synchronous and
broadcast-only: no database, no await, no path lookup. The target document is
resolved once per call, off the pump, as soon as its path is known. What is
drawn is projected on a short coalescing timer, and only to the viewers of an
open live document, so raw tool arguments are never broadcast. A preview never
gates the call: the lane applies the edit and the tool returns as it always
did; an ended call's late fragment or resolution is ignored.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from doceditor.document_scope import MAX_TEXT_BYTES
from doceditor.live_preview_args import ArgScanner
from doceditor.live_preview_slots import Slot, project_slots

if TYPE_CHECKING:
    from doceditor.live_runtime import LiveEntry

log = logging.getLogger(__name__)

COMMENT_TOOL = "doc_comment_create"
PREVIEW_TOOLS = frozenset({"edit_file", "multi_edit", "write_file", COMMENT_TOOL})
#: Broadcasts per call are coalesced to at most one per this interval.
FLUSH_SECONDS = 0.05
_ENDED_KEPT = 4096


def preview_tool(name: object) -> str | None:
    """The previewed tool a provider tool name calls, if any."""
    from tools.catalog import normalize_tool_name

    if not isinstance(name, str):
        return None
    tool = normalize_tool_name(name)
    return tool if tool in PREVIEW_TOOLS else None


@dataclass
class _Call:
    call_id: str
    thread_id: str
    turn_id: str | None
    tool: str
    workspace: Path | None
    scanner: ArgScanner = field(default_factory=ArgScanner)
    size: int = 0
    entry: LiveEntry | None = None
    name: str | None = None
    resolving: bool = False
    timer: asyncio.TimerHandle | None = None
    slots: dict[int, Slot] = field(default_factory=dict)
    shown: bool = False


class PreviewRoute:
    """Every in-flight previewable call, keyed by its provider call id."""

    def __init__(self) -> None:
        self.calls: dict[str, _Call] = {}
        self.ended: OrderedDict[str, None] = OrderedDict()
        self.loop: asyncio.AbstractEventLoop | None = None

    def start(self, call_id: object, *, thread_id: str, turn_id: str | None, tool: str,
              workspace: Path | None) -> None:
        """Open a call; a repeated or already-ended id is ignored."""
        if not isinstance(call_id, str) or not call_id or call_id in self.calls or call_id in self.ended:
            return
        self.loop = asyncio.get_running_loop()
        self.calls[call_id] = _Call(call_id, thread_id, turn_id, tool, workspace)

    def update(self, call_id: object, fragment: object) -> None:
        """Feed one input fragment; parsing is incremental and the broadcast is deferred."""
        call = self.calls.get(call_id) if isinstance(call_id, str) else None
        if call is None or not isinstance(fragment, str) or not fragment:
            return
        call.size += len(fragment.encode("utf-8"))
        if call.size > MAX_TEXT_BYTES:
            self.end(call.call_id, "capped")
            return
        try:
            call.scanner.feed(fragment)
        except Exception:
            # Presentation only: a parse error ends this preview and never reaches the stream that fed it.
            log.warning("Preview input could not be parsed; the settled write shows instead", exc_info=True)
            call.scanner.failed = True
        if call.scanner.failed:
            self.end(call.call_id, "malformed")
            return
        target = _target(call)
        if call.entry is None and not call.resolving and target is not None:
            call.resolving = True
            resolve = self._resolve_comment(call, *target) if call.tool == COMMENT_TOOL else self._resolve(call, target[1])
            asyncio.get_running_loop().create_task(resolve, name=f"preview-resolve:{call.call_id}")
        self._schedule(call)

    def end(self, call_id: object, outcome: str) -> None:
        """Close a call for good: its preview is removed, and later frames for it are ignored."""
        call = self.calls.pop(call_id, None) if isinstance(call_id, str) else None
        if call is None:
            return
        self.ended[call.call_id] = None
        while len(self.ended) > _ENDED_KEPT:
            self.ended.popitem(last=False)
        if call.timer is not None:
            call.timer.cancel()
        if call.shown and call.entry is not None:
            _send(call.entry, {"type": "doc_preview_end", "call_id": call.call_id, "thread_id": call.thread_id,
                               "outcome": outcome})

    def end_where(self, outcome: str, *, thread_id: str | None = None, turn_id: str | None = None) -> None:
        """End every call of one thread or one Turn: the backstop no call outlives."""
        for call in tuple(self.calls.values()):
            if (thread_id is not None and call.thread_id == thread_id) or (turn_id is not None and call.turn_id == turn_id):
                self.end(call.call_id, outcome)

    def _schedule(self, call: _Call) -> None:
        if call.timer is None and call.entry is not None:
            call.timer = asyncio.get_running_loop().call_later(FLUSH_SECONDS, self._flush, call.call_id)

    async def _resolve(self, call: _Call, path: str) -> None:
        from doceditor.live_publication_record import WriteActor
        from doceditor.live_tools import find_live
        from infra.db_async import run_in_db
        from infra.queries import get_thread
        from tools import filesystem_gate

        # The tool's own gates, unconsumed: the ordinary path boundary, then the document's owner-only rule.
        # A write that would be refused, or would need a host permit, is never previewed.
        try:
            try:
                resolved = await asyncio.to_thread(filesystem_gate._resolve_path, path, call.workspace, for_write=True)
            except PermissionError:
                resolved = None
            entry = find_live(resolved) if resolved is not None else None
            thread = await run_in_db(get_thread, call.thread_id) if entry is not None else None
            actor = WriteActor(call.thread_id, thread["project_id"] if thread else None)
            if resolved is None or (entry is not None and entry.policy is not None and not await entry.policy.admits(actor)):
                self.end(call.call_id, "refused")
                return
        except Exception:
            log.warning("Preview target resolution failed; the settled write shows instead", exc_info=True)
            return
        self._attach(call, entry, thread)

    async def _resolve_comment(self, call: _Call, key: str, value: str) -> None:
        from doceditor.live_preview_comments import resolve_comment_entry
        from infra.db_async import run_in_db

        try:
            entry, thread = await run_in_db(resolve_comment_entry, call.thread_id, call.workspace, key, value)
        except (PermissionError, ValueError):
            self.end(call.call_id, "refused")
            return
        except Exception:
            log.warning("Preview target resolution failed; the settled comment shows instead", exc_info=True)
            return
        self._attach(call, entry, thread)

    def _attach(self, call: _Call, entry: LiveEntry | None, thread) -> None:
        from doceditor.agent_name import agent_display_name

        if entry is None or self.calls.get(call.call_id) is not call:
            return  # not a live document, or the call ended while this ran
        call.entry = entry
        call.name = agent_display_name(thread["title"] if thread else None, call.thread_id)
        self._schedule(call)

    def _flush(self, call_id: str) -> None:
        call = self.calls.get(call_id)
        if call is None or call.entry is None:
            return
        call.timer = None
        live = call.entry.live
        text = live.text
        if call.tool == COMMENT_TOOL:
            from doceditor.live_preview_comments import project_comment

            kind, slots = project_comment(call.scanner, live, text, call.slots)
        else:
            kind, slots = "edit", project_slots(call.tool, call.scanner, text, call.slots)
        if not slots:
            return
        from doceditor.live_text import text_digest

        call.shown = True
        _send(call.entry, {"type": "doc_preview", "call_id": call.call_id, "thread_id": call.thread_id,
                           "name": call.name, "kind": kind, "digest": text_digest(text), "slots": slots})


def _target(call: _Call) -> tuple[str, str] | None:
    """The complete argument naming the call's document: a file tool's path, or a comment's target."""
    if call.tool == COMMENT_TOOL:
        from doceditor.live_preview_comments import comment_target

        return comment_target(call.scanner)
    path = call.scanner.string("path")
    return ("path", path.text()) if path is not None and path.complete else None


def _send(entry, frame: dict) -> None:
    live = entry.live
    try:
        live.preview_changed({"store_id": live.store.store_id, **frame})
    except Exception:
        log.warning("Preview broadcast failed; the settled write shows instead", exc_info=True)


previews = PreviewRoute()


def turn_ended(turn_id: str, outcome: str = "turn_ended") -> None:
    """End a finished Turn's previews from any thread; a no-op when none are open."""
    route, loop = previews, previews.loop
    if not route.calls or loop is None or loop.is_closed():
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        route.end_where(outcome, turn_id=turn_id)
    else:
        loop.call_soon_threadsafe(lambda: route.end_where(outcome, turn_id=turn_id))
