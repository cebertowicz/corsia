"""Ephemeral document-activity frames derived from settled tool calls."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

from doceditor import comments
from doceditor.agent_name import agent_display_name
from doceditor.document_scope import MAX_TEXT_BYTES
import infra.config as config
from infra.time import utc_now_iso
from registry import models as registry_models
from registry.service import resolve_file_identity
from tools.catalog import normalize_tool_name


# Canonical-name-keyed: harness naming differences are resolved before lookup.
DOCUMENT_ACTIVITY_TOOLS: dict[str, str] = {
    "read_file": "read",
    "write_file": "edit",
    "edit_file": "edit",
    "multi_edit": "edit",
    "doc_comment_create": "comment",
    "doc_comment_update": "decision",
}

_CANCELLED = "[Tool execution was cancelled]"


@dataclass(frozen=True)
class DocumentActivity:
    """One owner-routed frame, kept separate from its transport metadata."""

    user_id: str
    frame: dict[str, Any]


def is_document_activity_tool(name: str | None) -> bool:
    """Whether a canonicalized tool can produce document activity."""
    return normalize_tool_name(name) in DOCUMENT_ACTIVITY_TOOLS


def publish_document_activity(activity: DocumentActivity) -> None:
    """Publish after the blocking observer has returned to the event loop."""
    from infra.state import broadcast_to_user

    broadcast_to_user(activity.user_id, activity.frame)


def _failed_result(result_text: str, is_error: bool) -> bool:
    return (
        is_error
        or result_text == _CANCELLED
        or result_text.startswith(("Error", "Warning"))
        or "cancelled by user interruption" in result_text
    )


def _absolute_path(raw_path: object) -> Path | None:
    if not isinstance(raw_path, str) or not raw_path:
        return None
    path = Path(raw_path).expanduser()
    return path if path.is_absolute() else config.WORKSPACE / path


def _artifact_for_file(args: dict[str, Any], db) -> dict[str, Any] | None:
    path = _absolute_path(
        args.get("path") or args.get("file_path") or args.get("doc_path")
    )
    if path is not None:
        from doceditor.live_tools import find_live
        entry = find_live(path)
        if entry is not None:
            return registry_models.get_artifact(entry.artifact_id, db=db)
        return resolve_file_identity(str(path), db=db)
    return None


def _result_comment(result_text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(result_text)
    except (TypeError, ValueError):
        return None
    row = payload.get("comment") if isinstance(payload, dict) else None
    return row if isinstance(row, dict) else None


def _comment_context(
    name: str, args: dict[str, Any], result_text: str, db,
) -> tuple[dict[str, Any], dict[str, Any], str] | None:
    if name == "doc_comment_update" and not args.get("decision"):
        return None
    result_row = _result_comment(result_text)
    comment_id = (
        result_row.get("id") if result_row is not None else args.get("comment_id")
    )
    if not isinstance(comment_id, str) or not comment_id:
        return None
    row = comments.get_comment(comment_id, db=db)
    if row is None:
        return None
    artifact = registry_models.get_artifact(row["artifact_id"], db=db)
    if artifact is None or artifact.get("state") != "active":
        return None
    kind = (
        "decision"
        if name == "doc_comment_update"
        else "suggestion" if row.get("replacement") is not None else "comment"
    )
    return artifact, row, kind


def _read_document(artifact: dict[str, Any]) -> tuple[str, str] | None:
    path = _absolute_path(artifact.get("path"))
    if path is None:
        return None
    from doceditor.live_tools import find_live
    entry = find_live(path)
    if entry is not None:
        text = entry.live.snapshot[0]
        return text, hashlib.sha256(text.encode("utf-8")).hexdigest()
    try:
        # The editor cannot open larger files, so a frame for one could not render.
        if path.stat().st_size > MAX_TEXT_BYTES:
            return None
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeError):
        return None
    return text, hashlib.sha256(raw).hexdigest()


def _unique_span(text: str, value: object) -> dict[str, int] | None:
    if not isinstance(value, str) or not value or text.count(value) != 1:
        return None
    start = text.index(value)
    return {"from": start, "to": start + len(value)}


def _multi_edit_span(text: str, edits: object) -> dict[str, int] | None:
    if not isinstance(edits, list) or not edits:
        return None
    spans = [
        _unique_span(text, edit.get("new_string"))
        for edit in edits
        if isinstance(edit, dict)
    ]
    if len(spans) != len(edits) or any(span is None for span in spans):
        return None
    located = [span for span in spans if span is not None]
    return {
        "from": min(span["from"] for span in located),
        "to": max(span["to"] for span in located),
    }


def _read_span(text: str, args: dict[str, Any]) -> dict[str, int] | None:
    offset, limit = args.get("offset"), args.get("limit")
    if offset is None and limit is None:
        return None
    lines = text.splitlines(keepends=True)
    start_line = (offset - 1) if isinstance(offset, int) and offset else 0
    end_line = (
        start_line + limit
        if isinstance(limit, int) and limit
        else len(lines)
    )
    start_line = max(0, min(start_line, len(lines)))
    end_line = max(start_line, min(end_line, len(lines)))
    start = sum(len(line) for line in lines[:start_line])
    end = start + sum(len(line) for line in lines[start_line:end_line])
    return {"from": start, "to": end}


def _file_span(name: str, text: str, args: dict[str, Any]) -> dict[str, int] | None:
    if name == "read_file":
        return _read_span(text, args)
    if name == "write_file":
        return {"from": 0, "to": len(text)}
    if name == "edit_file":
        return _unique_span(text, args.get("new_string"))
    if name == "multi_edit":
        return _multi_edit_span(text, args.get("edits"))
    return None


def observe_document_activity(
    thread_id: str,
    name: str | None,
    args: dict[str, Any],
    result_text: str,
    is_error: bool,
    db,
) -> DocumentActivity | None:
    """Return one owner-routed frame without registering or publishing anything."""
    canonical_name = normalize_tool_name(name)
    declared_kind = DOCUMENT_ACTIVITY_TOOLS.get(canonical_name)
    if declared_kind is None or _failed_result(result_text, is_error):
        return None

    comment_id: str | None = None
    try:
        receipt = json.loads(result_text)
    except (TypeError, ValueError):
        receipt = None
    live_receipt = (receipt if isinstance(receipt, dict) and receipt.get("live_document") is True
                    and isinstance(receipt.get("digest"), str) else None)
    if canonical_name.startswith("doc_comment_"):
        context = _comment_context(canonical_name, args, result_text, db)
        if context is None:
            return None
        artifact, comment, kind = context
        comment_id = comment["id"]
        span = (
            {"from": comment["from_off"], "to": comment["to_off"]}
            if comment.get("from_off") is not None
            and comment.get("to_off") is not None
            else None
        )
    else:
        file_artifact = (registry_models.get_artifact(live_receipt.get("artifact_id"), db=db)
                         if live_receipt else _artifact_for_file(args, db))
        if file_artifact is None:
            return None
        artifact = file_artifact
        kind = declared_kind
        span = None

    thread = db.execute(
        "SELECT user_id, title FROM threads WHERE id=?", (thread_id,)
    ).fetchone()
    if thread is None or not thread["user_id"]:
        return None
    document = ("", live_receipt["digest"]) if live_receipt else _read_document(artifact)
    if document is None:
        return None
    text, revision = document
    if not canonical_name.startswith("doc_comment_"):
        from doceditor.live_tools import find_live
        path = _absolute_path(artifact.get("path"))
        live_edit = path is not None and find_live(path) is not None and canonical_name != "read_file"
        span = None if live_receipt or live_edit else _file_span(canonical_name, text, args)

    agent = agent_display_name(thread["title"], thread_id)
    return DocumentActivity(
        user_id=thread["user_id"],
        frame={
            "type": "doc_activity",
            "artifact_id": artifact["id"],
            "thread_id": thread_id,
            "agent": agent,
            "kind": kind,
            "span": span,
            "comment_id": comment_id,
            "revision": revision,
            "at": utc_now_iso(),
        },
    )
