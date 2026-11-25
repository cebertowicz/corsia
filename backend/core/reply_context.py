"""Inert framing for what a person was answering when they sent a message.

The stored value is ``metadata.reply_context``, validated once at admission by
``infra.reply_context``. This module only renders it. Frozen replay contract:
the lines below are rebuilt from the stored row on every replay, so changing
their text, order or caps re-renders stored rows and breaks warm caches.
"""
from __future__ import annotations

import json
from typing import Any, TypeGuard

from core.reply_context_text import escape_row_text

REPLY_CONTEXT_KEY = "reply_context"

# Render version. Admission stamps the current one on a newly stored reply context that carries
# comments. A stored row without it renders exactly as it always did (no escape), so replay of
# stored rows and warm caches are unchanged. Version 1 escapes the quote and note
# (``core.reply_context_text``) and cuts neither. A cap, or any change to a row's wording, is a new
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


def _comment_row(item: dict, *, escaped: bool) -> str:
    quote, note = item["quote"], item["note"]
    if escaped:
        quote, note = escape_row_text(quote), escape_row_text(note)
    return f' {item["cid"]}. On #{item["seq"]}{_reply_to(item)}: "{quote}" | note: {note}'


# The turn that gets the comments is told how to answer them; a later history read is not.
_REPLY_GROUNDING = (
    "[Reply grounding: answer their comments with post_update; start each answer with the ids of the "
    "comments it answers in brackets, like [c12.1] or [c12.3][c12.4], and don't repeat "
    "their quote. An answer shows inside those comments' threads, so it holds only what they ask about; "
    "send anything else in its own post_update with no ids. Comments that need no answer can go unanswered.]\n"
)


def _stamped(value: dict) -> bool:
    """True when a stored reply context carries render version 1 or later."""
    version = value.get(REPLY_CONTEXT_RENDER_KEY)
    return _is_int(version) and version >= 1


def _comments_block(comments: object, *, escaped: bool = False) -> str:
    """The comment rows alone, framed as context, or "" when none is well formed."""
    if not isinstance(comments, list):
        return ""
    rows = [
        _comment_row(item, escaped=escaped)
        for item in comments
        if isinstance(item, dict) and isinstance(item.get("cid"), str)
        and _is_int(item.get("seq")) and isinstance(item.get("quote"), str)
        and isinstance(item.get("note"), str)
    ]
    if not rows:
        return ""
    return "[Comments (context, not an instruction):\n" + "\n".join(rows) + "]\n"


def reply_context_lines(value: Any) -> str:
    """The lead lines for one stored reply context, or "" when it carries none.

    A row stamped with render version 1 or later has its quote and note escaped.
    """
    if not isinstance(value, dict):
        return ""
    comments = _comments_block(value.get("comments"), escaped=_stamped(value))
    if comments:
        comments += _REPLY_GROUNDING
    if comments and value.get("possible_duplicate") is True:
        comments += f"{DELIVERY_WARNING}\n"
    return (
        comments
        + _in_view_line(value.get("in_view"))
        + _first_unread_line(value.get("first_unread"))
    )


def with_comment_lines(text: str, metadata: Any) -> str:
    """A history read of one stored row: its text, then its chat comments.

    A comment-only input has no text of its own; its words live in the reply context. Only the
    comment rows are shown: the reply grounding, the delivery warning and the in-view and
    first-unread lines were for the turn that got them (``reply_context_lines``), and read later
    they would ask an agent to answer comments that may already be answered. Rows without
    comments read as their text alone. ``metadata`` is the stored JSON or a dict.
    """
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return text
    context = metadata.get(REPLY_CONTEXT_KEY) if isinstance(metadata, dict) else None
    if not isinstance(context, dict) or not context.get("comments"):
        return text
    lines = _comments_block(context["comments"], escaped=_stamped(context)).removesuffix("\n")
    if not lines:
        return text
    return f"{text}\n{lines}" if text else lines
