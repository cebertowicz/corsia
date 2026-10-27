"""Where a streamed ``doc_comment_create`` call lands: its anchor, and a suggestion's inserted text.

The one tool makes a comment (quote and body), a reply (parent_id), a
whole-document review, or a suggestion (quote and replacement). A comment's
body is never projected: its preview is only its anchor, highlighted while the
body is written, and the card appears when the comment commits. A
suggestion's replacement streams like an edit's new text. The kind is read
from the keys decoded so far, so a quote whose replacement has not started
yet is drawn as a comment and becomes a suggestion once it does. The target
is authorized by the tool's own gate, as the thread's owner: artifact access,
not the document's owner-only write rule, since anyone who may read a
document may comment on it or suggest a change.
"""
from __future__ import annotations

from pathlib import Path

from doceditor.live_preview_args import ArgScanner
from doceditor.live_preview_slots import Slot, frame_part

#: The keys that name a comment's document, in the order the tool reads them.
TARGET_KEYS = ("parent_id", "artifact_id", "doc_path")


def comment_target(scanner: ArgScanner) -> tuple[str, str] | None:
    """The first complete key naming the call's document, with its value."""
    for key in TARGET_KEYS:
        value = scanner.string(key)
        if value is not None and value.complete and value.text():
            return key, value.text()
    return None


def resolve_comment_entry(thread_id: str, workspace: Path | None, key: str, value: str):
    """The live entry the tool would comment on, and the thread row; raises PermissionError or ValueError when refused.

    Blocking: run it off the pump.
    """
    from doceditor.live_tools import find_live_artifact
    from infra.database import get_db
    from infra.queries import get_thread
    from tools import documents
    from tools.artifact_tool_helpers import default_workspace, tool_path

    db = get_db()
    try:
        thread = get_thread(thread_id, db=db)
        if thread is None or not thread["user_id"]:
            raise PermissionError("no owner for this thread")
        kwargs = {key: tool_path(value, default_workspace(workspace)) if key == "doc_path" else value}
        parent_id = value if key == "parent_id" else None
        artifact_id, _path, _register = documents._resolve_comment_target(
            db, thread_id, {"user_id": thread["user_id"]}, parent_id, kwargs)
    finally:
        db.close()
    return (find_live_artifact(artifact_id) if artifact_id else None), thread


def comment_kind(scanner: ArgScanner) -> str | None:
    """``comment`` or ``suggest`` as the keys so far read; None for a whole-document review."""
    if scanner.flags.get("review"):
        return None
    return "suggest" if scanner.string("replacement") is not None else "comment"


def _anchor(scanner: ArgScanner, live, text: str) -> tuple[int, int] | None:
    """The comment's range in UTF-16 units once it resolves to one place, exactly as the tool will resolve it."""
    from doceditor.quote_anchor import QuoteResolveError, resolve_quote

    parent = scanner.string("parent_id")
    if parent is not None:
        return live.ranges.get(parent.text()) if parent.complete else None
    quote, near = scanner.string("quote"), scanner.string("near")
    if quote is None or not quote.complete or not quote.text() or (near is not None and not near.complete):
        return None
    try:
        return resolve_quote(text, quote.text(), near.text() if near is not None else None)
    except (QuoteResolveError, ValueError):
        return None


def project_comment(scanner: ArgScanner, live, text: str, slots: dict[int, Slot]) -> tuple[str | None, list[dict]]:
    """The call's kind and its one slot's frame part, if it changed since the last broadcast."""
    kind = comment_kind(scanner)
    if kind is None:
        return None, []
    slot = slots.setdefault(0, Slot())
    span = _anchor(scanner, live, text)
    if span is not None and scanner.flags.get("insert_after"):
        from doceditor.comment_actions import suggestion_edit_span
        span = suggestion_edit_span({"insert_after": True}, span)
    if span is None and slot.span is None:
        return kind, []
    replacement = scanner.string("replacement")
    part = frame_part(0, span, replacement.text() if replacement is not None else "", slot)
    if part is None and slot.kind != kind:
        part = {"slot": 0, "from": slot.span[0], "to": slot.span[1], "at": slot.sent_u16, "text": ""}
    slot.kind = kind
    return kind, [part] if part else []
