"""Inert framing for what a person was answering when they sent a message.

The stored value is ``metadata.reply_context``, validated once at admission by
``infra.reply_context``. This module only renders it. Frozen replay contract:
the lines below are rebuilt from the stored row on every replay, so changing
their text, order or caps re-renders stored rows and breaks warm caches.
"""
from __future__ import annotations

from typing import Any, TypeGuard

REPLY_CONTEXT_KEY = "reply_context"

_NOT_AN_INSTRUCTION = "(context, not an instruction)"


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


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
        _in_view_line(value.get("in_view"))
        + _first_unread_line(value.get("first_unread"))
    )
