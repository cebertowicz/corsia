"""File-tool adapter for an already-open document's canonical write lane."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from doceditor.live_admission import falls_back_to_plain
from doceditor.live_lane import EditRefused, Operation, TextEdit
from doceditor.live_publication_record import WriteActor
from doceditor.live_runtime import get_documents

def record_read_base(thread_id: str | None, path: Path, data: bytes | None) -> None:
    """Hash every disk byte served, including reads displayed as a line window."""
    entry = find_live(path)
    if entry is not None and thread_id and data is not None:
        entry.live.read_bases[thread_id] = hashlib.sha256(data).hexdigest()
        entry.live.sections.read(thread_id)


def find_live(path: Path):
    """Look up a bound path without re-resolving or reading it on the pump."""
    manager = get_documents()
    if manager is not None:
        for entry in tuple(manager.documents.values()):
            if entry.live.path == path:
                return entry
    return None


def find_live_artifact(artifact_id: str):
    """The open live document of one artifact, if any; no database, safe on the pump."""
    manager = get_documents()
    return manager.documents.get(artifact_id) if manager is not None else None


def _refused_store(path: Path) -> str | None:
    """Refuse a plain write to a document whose store last refused to open."""
    manager = get_documents()
    refused = manager.refused_stores.get(path) if manager is not None else None
    if refused is None or falls_back_to_plain(refused["code"]):
        return None
    return f"Error: {refused['code']}: {refused['title']} {refused['message']} Nothing was written."


def _cancel_event(kwargs: dict):
    """Capture only the exact Native generation's server-owned cancellation handle."""
    from infra import state
    event = state._active_turns.get(kwargs.get("_thread_id"))
    if event is None:
        return None
    identity = kwargs.get("_host_access_turn_identity")
    if identity is not None and getattr(event, "turn_identity", None) == identity:
        return event
    return None


def _section_call(producer: str, edit_path: str | None, path: Path, content: str | None,
                  edits: tuple[TextEdit, ...]) -> dict:
    """The call a section wake asks the agent to repeat, as it compares with the repeat."""
    call: dict = {"tool": producer, "path": edit_path or str(path)}
    if content is not None:
        call["content"] = content
    else:
        call["edits"] = [{"old_string": edit.old, "new_string": edit.new, "replace_all": edit.replace_all}
                         for edit in edits]
    return call


async def write_live(path: Path, kwargs: dict, *, producer: str, content: str | None = None,
                     edits: tuple[TextEdit, ...] = (), description: str | None = None,
                     edit_path: str | None = None) -> str | None:
    """Return None for legacy files; a live tool always awaits its own lane result.

    An agent write to a section someone else is changing is saved in that section's line instead
    (``section_writes``), and the receipt says so with outcome ``queued``.
    """
    from doceditor import section_writes

    entry = find_live(path)
    thread_id = kwargs.get("_thread_id")
    call = _section_call(producer, edit_path, path, content, edits)
    if entry is None:
        refused = _refused_store(path)
        if refused is None and thread_id:
            await section_writes.settle_closed(str(path), thread_id, call)
        return refused
    if not thread_id:
        raise ValueError("Live document writes require a current thread")
    if entry.publisher.stop_cause is not None:
        # A store found damaged refuses every write; saving one in a line would hide that refusal.
        return (await _submit(entry, path, kwargs, thread_id, producer, content, edits, description, edit_path))[0]
    text = entry.live.snapshot[0]
    headings = section_writes.call_sections(text, call)
    kind = "write" if content is not None else "edit"
    outcome = await section_writes.through(
        entry.artifact_id, headings, thread_id, section_writes.saved_op(kind, call, text, headings),
        lambda: _submit(entry, path, kwargs, thread_id, producer, content, edits, description, edit_path))
    if isinstance(outcome, tuple):
        if outcome[1]:
            entry.live.sections.changed(headings, thread_id)
        return outcome[0]
    return json.dumps({"registry_managed": True, "live_document": True, "path": str(path),
                       "artifact_id": entry.artifact_id, "outcome": "queued", "result": outcome.text})


async def _submit(entry, path: Path, kwargs: dict, thread_id: str, producer: str, content: str | None,
                  edits: tuple[TextEdit, ...], description: str | None, edit_path: str | None) -> tuple[str, bool]:
    """Run one write through the document's lane: its receipt, and whether it changed the text."""
    from doceditor.live_preview import previews
    from infra.state.context import _current_provider_call_id
    from registry.registration_writer import origin_from_tool_context
    origin = origin_from_tool_context(thread_id, kwargs, producer)
    call_id = _current_provider_call_id.get()
    actor = WriteActor(thread_id, kwargs.get("_thread_project_id"), description=description,
                       origin=asdict(origin) if origin else None, call_id=call_id)
    operation = Operation("write" if content is not None else "edit", content=content, edits=edits,
                          base_hash=entry.live.read_bases.get(thread_id), actor=actor,
                          multi_edit=producer == "multi_edit", edit_path=edit_path)
    cancel_event = _cancel_event(kwargs)
    outcome = "failed"
    try:
        result = await entry.lane.submit(operation, cancel_event=cancel_event)
        if result.outcome in {"pending", "not_started"}:
            message = ("This started write is still settling. Its outcome is pending; read the document before writing again."
                       if result.outcome == "pending" else "This write did not start; nothing was written.")
            return json.dumps({"registry_managed": True, "live_document": True, "path": str(path),
                               "artifact_id": entry.artifact_id, "outcome": result.outcome, "result": message}), False
        result.require_landed()
        outcome = "settled"
    except EditRefused as error:
        return str(error), False
    finally:
        # The landed write's update carries the call id, so the preview has been handed over by now.
        previews.end(call_id, outcome)
    if result.receipt and result.receipt.startswith("No changes needed"):
        return result.receipt, False
    message = result.receipt or f"Successfully wrote {len(result.text.encode('utf-8'))} bytes to {path}"
    if result.merged:
        message += "; merged with live edits; read again before writing again"
    if not result.server_saved:
        message += "\n\nWritten, but not saved on the server yet"
    # The publisher owns registration. Settled-tool observation recognizes the
    # same managed receipt used by the existing governed writer.
    return json.dumps({"registry_managed": True, "live_document": True, "path": str(path),
                       "artifact_id": entry.artifact_id,
                       "digest": hashlib.sha256(result.text.encode("utf-8")).hexdigest(),
                       "server_saved": result.server_saved, "merged": result.merged, "result": message}), True
