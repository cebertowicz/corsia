"""Byte-exact tool-result ownership and bounded model-facing projection."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import quote

from core.thread_lifecycle import TOOL_TRIM_MARKER
from core.token_estimation import _estimate_tokens


TOOL_RESULT_REFERENCE_VERSION = 1

# The size a tool result is projected at when it goes IN FRONT OF THE MODEL.
# A result larger than this is previewed with a reference the model can follow
# to retrieve the rest, so nothing is lost.
#
# It has exactly one job here: be the SAME number on the live path and on the
# rebuild path. It was a bare literal on the live path only, so when a harness
# session died and CORSIA rebuilt the conversation from its own database, the
# rebuild replayed every tool result at FULL stored size -- a 65KB file read
# that the model had only ever seen as a 4K preview came back whole. The rebuild
# then measured a conversation far larger than the one the model was actually
# holding, declared it did not fit, and refused it. Production thread 924c0f4d
# re-ran that refusal 568 times and died.
HARNESS_TOOL_RESULT_BUDGET_TOKENS = 4_096
_TOOL_RESULT_PREVIEW_LABEL = "CORSIA_TOOL_RESULT_PREVIEW"


class ToolResultProjectionError(RuntimeError):
    """A tool result cannot be projected without losing identity or fit truth."""


class ToolResultReferenceError(ToolResultProjectionError):
    """A tool-result reference does not resolve to its exact scoped payload."""


def _content_identity(content: str) -> tuple[str, int]:
    raw = content.encode("utf-8")
    return hashlib.sha256(raw).hexdigest(), len(raw)


def tool_result_integrity_metadata(content: str) -> dict[str, Any]:
    """Return the immutable byte identity stamped on a canonical tool row."""
    digest, byte_count = _content_identity(content)
    return {
        "tool_result_sha256": digest,
        "tool_result_byte_count": byte_count,
    }


def tool_result_reference(
    *,
    thread_id: str,
    message_id: str,
    sha256: str,
    byte_count: int,
) -> dict[str, Any]:
    """Build one immutable, thread-scoped reference to canonical message bytes."""
    if not thread_id or not message_id:
        raise ToolResultReferenceError("tool-result reference requires thread and message")
    if (
        len(sha256) != 64
        or sha256 != sha256.lower()
        or any(character not in "0123456789abcdef" for character in sha256)
    ):
        raise ToolResultReferenceError("tool-result reference has invalid digest")
    if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
        raise ToolResultReferenceError("tool-result reference has invalid byte count")
    encoded_thread = quote(thread_id, safe="")
    encoded_message = quote(message_id, safe="")
    return {
        "version": TOOL_RESULT_REFERENCE_VERSION,
        "kind": "corsia_tool_result",
        "thread_id": thread_id,
        "message_id": message_id,
        "sha256": sha256,
        "byte_count": byte_count,
        "retrieval_path": (
            f"corsia://threads/{encoded_thread}/messages/"
            f"{encoded_message}/tool-result"
        ),
    }


def _verified_message_identity(message: Any) -> tuple[str, int]:
    if getattr(message, "role", None) != "tool":
        raise ToolResultProjectionError("tool-result projection requires a tool message")
    content = str(getattr(message, "content", "") or "")
    actual_digest, actual_bytes = _content_identity(content)
    stamped_digest = getattr(message, "content_sha256", None)
    stamped_bytes = getattr(message, "content_byte_count", None)
    if stamped_digest is not None and stamped_digest != actual_digest:
        raise ToolResultProjectionError("tool-result content digest mismatch")
    if stamped_bytes is not None and stamped_bytes != actual_bytes:
        raise ToolResultProjectionError("tool-result content byte count mismatch")
    return actual_digest, actual_bytes


def _preview_marker(reference: dict[str, Any], *, omitted_characters: int) -> str:
    payload = {
        **reference,
        "omitted_characters": omitted_characters,
    }
    return (
        "\n\n["
        + _TOOL_RESULT_PREVIEW_LABEL
        + " reference="
        + json.dumps(
            payload,
            sort_keys=True,
            separators=(",\n", ":"),
            ensure_ascii=False,
        )
        + "]\n\n"
    )


def _preview_candidate(
    content: str,
    *,
    kept_characters: int,
    reference: dict[str, Any],
) -> str:
    head_characters = kept_characters // 2
    tail_characters = kept_characters - head_characters
    tail = content[-tail_characters:] if tail_characters else ""
    return (
        content[:head_characters]
        + _preview_marker(
            reference,
            omitted_characters=len(content) - kept_characters,
        )
        + tail
    )


def render_tool_result_for_model(
    thread_id: str,
    message: Any,
    *,
    budget_tokens: int | None,
) -> str:
    """Render exact bytes when they fit, otherwise a bounded immutable preview."""
    content = str(getattr(message, "content", "") or "")
    digest, byte_count = _verified_message_identity(message)
    if budget_tokens is None or _estimate_tokens(content) <= budget_tokens:
        return content
    if content.startswith(TOOL_TRIM_MARKER):
        raise ToolResultProjectionError(
            "destructively retained tool result cannot provide a retrieval reference"
        )
    if (
        isinstance(budget_tokens, bool)
        or not isinstance(budget_tokens, int)
        or budget_tokens <= 0
    ):
        raise ToolResultProjectionError(
            "tool-result preview requires a positive rendered budget"
        )
    reference = tool_result_reference(
        thread_id=thread_id,
        message_id=str(getattr(message, "message_id", "") or ""),
        sha256=digest,
        byte_count=byte_count,
    )
    marker_only = _preview_candidate(
        content,
        kept_characters=0,
        reference=reference,
    )
    if _estimate_tokens(marker_only) > budget_tokens:
        raise ToolResultProjectionError(
            "tool-result reference does not fit the rendered budget"
        )

    lower, upper = 0, len(content) - 1
    best = marker_only
    while lower <= upper:
        kept = (lower + upper) // 2
        candidate = _preview_candidate(
            content,
            kept_characters=kept,
            reference=reference,
        )
        if _estimate_tokens(candidate) <= budget_tokens:
            best = candidate
            lower = kept + 1
        else:
            upper = kept - 1
    return best


def resolve_tool_result_reference(
    reference: dict[str, Any],
    *,
    thread_id: str,
    db,
) -> str:
    """Resolve one reference only inside its bound thread and byte identity."""
    if not isinstance(reference, dict):
        raise ToolResultReferenceError("tool-result reference is not an object")
    if reference.get("version") != TOOL_RESULT_REFERENCE_VERSION:
        raise ToolResultReferenceError("tool-result reference version is unsupported")
    if reference.get("kind") != "corsia_tool_result":
        raise ToolResultReferenceError("tool-result reference kind is invalid")
    if reference.get("thread_id") != thread_id:
        raise ToolResultReferenceError("tool-result reference names another thread")
    message_id = reference.get("message_id")
    if not isinstance(message_id, str) or not message_id:
        raise ToolResultReferenceError("tool-result reference has no message identity")
    row = db.execute(
        "SELECT content,metadata FROM messages "
        "WHERE thread_id=? AND id=? AND role='tool'",
        (thread_id, message_id),
    ).fetchone()
    if row is None:
        raise ToolResultReferenceError("tool-result reference is missing")
    content = str(row["content"] or "")
    if content.startswith(TOOL_TRIM_MARKER):
        raise ToolResultReferenceError(
            "destructively retained tool result has no full payload"
        )
    digest, byte_count = _content_identity(content)
    if reference.get("sha256") != digest:
        raise ToolResultReferenceError("tool-result reference digest mismatch")
    if reference.get("byte_count") != byte_count:
        raise ToolResultReferenceError("tool-result reference byte count mismatch")
    try:
        metadata = json.loads(row["metadata"] or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ToolResultReferenceError("tool-result metadata is malformed") from exc
    stamped_digest = metadata.get("tool_result_sha256")
    stamped_bytes = metadata.get("tool_result_byte_count")
    if stamped_digest is not None and stamped_digest != digest:
        raise ToolResultReferenceError(
            "tool-result canonical integrity stamp does not match"
        )
    if stamped_bytes is not None and stamped_bytes != byte_count:
        raise ToolResultReferenceError(
            "tool-result canonical integrity stamp does not match"
        )
    expected = tool_result_reference(
        thread_id=thread_id,
        message_id=message_id,
        sha256=digest,
        byte_count=byte_count,
    )
    if reference.get("retrieval_path") != expected["retrieval_path"]:
        raise ToolResultReferenceError("tool-result retrieval path mismatch")
    return content


MAX_TOOL_RESULT_PAGE_BYTES = 65_536


def _bounded_page_value(value: object, label: str, *, positive: bool) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolResultReferenceError(f"tool-result {label} must be an integer")
    if value < 0 or (positive and value == 0):
        qualifier = "positive" if positive else "non-negative"
        raise ToolResultReferenceError(f"tool-result {label} must be {qualifier}")
    return value


def resolve_tool_result_page(
    *,
    thread_id: str,
    message_id: str,
    sha256: str,
    offset_bytes: int,
    limit_bytes: int,
    db,
) -> dict[str, Any]:
    """Read one UTF-8-safe byte-bounded page from a canonical scoped tool message."""
    offset = _bounded_page_value(offset_bytes, "offset", positive=False)
    limit = _bounded_page_value(limit_bytes, "limit", positive=True)
    if limit > MAX_TOOL_RESULT_PAGE_BYTES:
        raise ToolResultReferenceError(
            f"tool-result limit exceeds {MAX_TOOL_RESULT_PAGE_BYTES} bytes"
        )
    row = db.execute(
        "SELECT content,metadata FROM messages "
        "WHERE thread_id=? AND id=? AND role='tool'",
        (thread_id, message_id),
    ).fetchone()
    if row is None:
        raise ToolResultReferenceError(
            "tool-result message is missing or belongs to another thread"
        )
    content = str(row["content"] or "")
    if content.startswith(TOOL_TRIM_MARKER):
        raise ToolResultReferenceError(
            "destructively retained tool result has no canonical bytes"
        )
    raw = content.encode("utf-8")
    actual_digest = hashlib.sha256(raw).hexdigest()
    if sha256 != actual_digest:
        raise ToolResultReferenceError("tool-result digest mismatch")
    try:
        metadata = json.loads(row["metadata"] or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ToolResultReferenceError("tool-result metadata is malformed") from exc
    if (
        metadata.get("tool_result_sha256") != actual_digest
        or metadata.get("tool_result_byte_count") != len(raw)
    ):
        raise ToolResultReferenceError(
            "tool-result canonical integrity metadata mismatch"
        )
    if offset > len(raw):
        raise ToolResultReferenceError("tool-result offset exceeds byte count")
    try:
        raw[:offset].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolResultReferenceError(
            "tool-result offset is not a UTF-8 boundary"
        ) from exc

    end = min(len(raw), offset + limit)
    while end > offset:
        try:
            page = raw[offset:end].decode("utf-8")
            break
        except UnicodeDecodeError:
            end -= 1
    else:
        if offset == len(raw):
            page = ""
        else:
            raise ToolResultReferenceError(
                "tool-result limit cannot reach the next UTF-8 boundary"
            )
    return {
        "version": TOOL_RESULT_REFERENCE_VERSION,
        "kind": "corsia_tool_result_page",
        "thread_id": thread_id,
        "message_id": message_id,
        "sha256": actual_digest,
        "byte_count": len(raw),
        "offset_bytes": offset,
        "next_offset_bytes": end,
        "content": page,
        "complete": end == len(raw),
    }
