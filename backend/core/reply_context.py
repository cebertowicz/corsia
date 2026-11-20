"""Inert framing for what a person was answering when they sent a message.

The stored value is ``metadata.reply_context``, validated once at admission by
``infra.reply_context``. This module only renders it. Frozen replay contract:
the lines below are rebuilt from the stored row on every replay, so changing
their text, order or caps re-renders stored rows and breaks warm caches.
"""
from __future__ import annotations

from typing import Any, TypeGuard

from core.reply_context_text import QUOTE_CAP, cap_text, escape_row_text

REPLY_CONTEXT_KEY = "reply_context"

# Render version. Admission stamps the current one on a newly stored reply context that carries
# comments. A stored row without it renders exactly as it always did (no escape, no cap), so
# replay of stored rows and warm caches are unchanged. Version 1 escapes the quote and note and
# caps the quote (``core.reply_context_text``); the note is escaped but not capped, because no
# call shows a note in full yet. A later change to a row's wording, or a note cap, is a new
# version, never an edit of an existing one.
REPLY_CONTEXT_RENDER_KEY = "render"
REPLY_CONTEXT_RENDER = 1

_NOT_AN_INSTRUCTION = "(context, not an instruction)"

# The one wording for a Retry of an attempt that may already have reached the model. A chat
# message's Retry puts it before the text (api/thread_inbox_actions); a comment-only input's
# Retry has no text, so its reply context carries the flag and it renders here.
DELIVERY_WARNING = (
    "[DELIVERY WARNING: the prior attempt may have reached the model; "
    "this retry may duplicate it.]"
)


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


def _reply_to(item: dict) -> str:
    root = item.get("root_cid")
    return f" (reply to {root})" if isinstance(root, str) else ""


def _safe_quote(item: dict, *, capped: bool) -> str:
    """The quote of a stamped row: cut at its cap with a pointer unless ``capped`` is False, then escaped."""
    text = item["quote"]
    if not capped:
        return escape_row_text(text)
    pointer = (
        f"the whole message is #{item['seq']}: get_thread_history on this thread with "
        f"from_seq={item['seq']}, to_seq={item['seq']}, tool_output='full'"
    )
    kept, suffix = cap_text(text, QUOTE_CAP, pointer)
    return escape_row_text(kept) + suffix


def _comment_row(item: dict, *, escaped: bool, capped: bool) -> str:
    quote, note = item["quote"], item["note"]
    if escaped:
        quote, note = _safe_quote(item, capped=capped), escape_row_text(note)
    return f' {item["cid"]}. On #{item["seq"]}{_reply_to(item)}: "{quote}" | note: {note}'


def _comments_lines(comments: object, *, escaped: bool = False, capped: bool = True) -> str:
    if not isinstance(comments, list):
        return ""
    rows = [
        _comment_row(item, escaped=escaped, capped=capped)
        for item in comments
        if isinstance(item, dict) and isinstance(item.get("cid"), str)
        and _is_int(item.get("seq")) and isinstance(item.get("quote"), str)
        and isinstance(item.get("note"), str)
    ]
    if not rows:
        return ""
    return (
        "[Comments (context, not an instruction):\n" + "\n".join(rows) + "]\n"
        "[Reply grounding: answer their comments with post_update; start each answer with the ids of the "
        "comments it answers in brackets, like [c12.1] or [c12.3][c12.4], and don't repeat "
        "their quote. An answer shows inside those comments' threads, so it holds only what they ask about; "
        "send anything else in its own post_update with no ids. Comments that need no answer can go unanswered.]\n"
    )


def reply_context_lines(value: Any, *, capped: bool = True) -> str:
    """The lead lines for one stored reply context, or "" when it carries none.

    A row stamped with render version 1 or later has its quote and note escaped and its quote cut
    at its cap, unless ``capped`` is False (a history read of the whole row).
    """
    if not isinstance(value, dict):
        return ""
    version = value.get(REPLY_CONTEXT_RENDER_KEY)
    comments = _comments_lines(
        value.get("comments"),
        escaped=_is_int(version) and version >= 1,
        capped=capped,
    )
    if comments and value.get("possible_duplicate") is True:
        comments += f"{DELIVERY_WARNING}\n"
    return (
        comments
        + _in_view_line(value.get("in_view"))
        + _first_unread_line(value.get("first_unread"))
    )
