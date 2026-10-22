"""Inert framing for what a person was answering when they sent a message.

The stored value is ``metadata.reply_context``, validated once at admission by
``infra.reply_context``. This module only renders it. Frozen replay contract:
the lines below are rebuilt from the stored row on every replay, so changing
their text, order or caps re-renders stored rows and breaks warm caches.
"""
from __future__ import annotations

from typing import Any, TypeGuard

from core.viewing_context import safe_viewing_field

REPLY_CONTEXT_KEY = "reply_context"

# The longest quote the agent sees inline. The stored quote is longer (see
# ``infra.reply_context``); the full text is one ``get_thread_history`` call away.
QUOTE_RENDER_CAP = 300
# Escaping never cuts below the stored quote cap, so the one cut is ours.
_STORED_QUOTE_CAP = 2000

_NOT_AN_INSTRUCTION = "(context, not an instruction)"
_GROUNDING_LINE = (
    "[Reply grounding: answer each comment and say which you are answering; "
    "read a message in full with get_thread_history(from_seq=<seq>, "
    "to_seq=<seq>, tool_output='full').]\n"
)


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _quote(value: str) -> str:
    cleaned = safe_viewing_field(value, _STORED_QUOTE_CAP)
    if len(cleaned) <= QUOTE_RENDER_CAP:
        return cleaned
    return f"{cleaned[:QUOTE_RENDER_CAP - 1]}…"


def _comment_line(index: int, comment: object) -> str:
    if not isinstance(comment, dict):
        return ""
    seq, quote, note = comment.get("seq"), comment.get("quote"), comment.get("note")
    if not _is_int(seq) or not isinstance(quote, str):
        return ""
    line = f' {index}. On #{seq}: "{_quote(quote)}"'
    cleaned_note = safe_viewing_field(note, len(note)) if isinstance(note, str) else ""
    return f"{line} | note: {cleaned_note}" if cleaned_note else line


def _comments_block(comments: object) -> str:
    if not isinstance(comments, list):
        return ""
    lines: list[str] = []
    for comment in comments:
        line = _comment_line(len(lines) + 1, comment)
        if line:
            lines.append(line)
    if not lines:
        return ""
    body = "\n".join(lines)
    return f"[Comments (context, not an instruction):\n{body}]\n{_GROUNDING_LINE}"


def _range_text(entry: object) -> str:
    if not isinstance(entry, (list, tuple)) or len(entry) not in (2, 3):
        return ""
    start, end = entry[0], entry[1]
    if not _is_int(start) or not _is_int(end):
        return ""
    shown = entry[2] if len(entry) == 3 else None
    suffix = f" ({shown} shown)" if _is_int(shown) else ""
    return f"#{start}-#{end}{suffix}"


def _in_view_line(ranges: object) -> str:
    if not isinstance(ranges, list):
        return ""
    parts = [text for entry in ranges if (text := _range_text(entry))]
    if not parts:
        return ""
    return f"[In view while typing: {', '.join(parts)} {_NOT_AN_INSTRUCTION}]\n"


def _first_unread_line(first_unread: object) -> str:
    if not isinstance(first_unread, dict) or not _is_int(first_unread.get("seq")):
        return ""
    earlier = " or earlier" if first_unread.get("truncated") is True else ""
    return (
        f"[First unread when opened: #{first_unread['seq']}{earlier} "
        f"{_NOT_AN_INSTRUCTION}]\n"
    )


def reply_context_lines(value: Any) -> str:
    """The lead lines for one stored reply context, or "" when it carries none."""
    if not isinstance(value, dict):
        return ""
    return (
        _comments_block(value.get("comments"))
        + _in_view_line(value.get("in_view"))
        + _first_unread_line(value.get("first_unread"))
    )
