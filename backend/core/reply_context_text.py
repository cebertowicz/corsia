"""Escaping and capping for the person's words inside a chat-comment row (render version 1).

``core.reply_context`` puts each comment on one row, ``cid. On #seq: "quote" | note: note``. A quote
is the agent's own text and a note is the person's, so either can hold what looks like a CORSIA
line (``]``, a line break, ``[Reply grounding: ...]``) or be long enough to fill a turn. Rendering
fixes both without changing what is stored.

Escape (``escape_row_text``), lossless and in this order:

- ``\\`` becomes ``\\\\``, then ``"`` becomes ``\\"``, ``[`` becomes ``\\[`` and ``]`` becomes ``\\]``;
- every line break (``\\r\\n``, ``\\r``, ``\\n``, U+0085, U+2028, U+2029) becomes the two characters
  ``\\n``;
- every other C0 control character except tab is dropped.

The backslash goes first so a typed ``\\n`` stays distinguishable from a line break. A row stays one
line and no text in it can open or close a bracketed line or end the quote's double quotes.

Cap (``cap_text``): the quote is cut at ``QUOTE_CAP`` characters, measured before escaping, so a
cut never splits an escape, and it never splits a surrogate pair. A cut ends with
``… (N more characters; <pointer>)`` where ``<pointer>`` names the call that shows the whole
agent message. The note is NOT capped in version 1: a cap needs a pointer that is true, and no
call can show a comment's note in full yet. A later render version can add a note cap once one
can. The cap is part of render version 1: changing it, or adding one, is a new render version
(``core.reply_context.REPLY_CONTEXT_RENDER``), never an edit in place, because stored rows are
re-rendered on every replay.
"""
from __future__ import annotations

import re

# The quote is the agent's own text, and the whole message is readable by its seq.
# STATED GUESS: 1,000 characters is well above a margin quote; no agent route reads production
# comment sizes, so the figure is not measured.
QUOTE_CAP = 1000

_LINE_BREAK = re.compile("\r\n|[\r\n\x85  ]")
# C0 controls other than tab (\x09); \n and \r are already gone by the time this runs.
_DROPPED = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")


def escape_row_text(text: str) -> str:
    """The text as one safe line: see the module docstring for the rules and their order."""
    escaped = (
        text.replace("\\", "\\\\").replace('"', '\\"').replace("[", "\\[").replace("]", "\\]")
    )
    return _DROPPED.sub("", _LINE_BREAK.sub(lambda _: "\\n", escaped))


def cap_text(text: str, limit: int, pointer: str) -> tuple[str, str]:
    """The text cut to ``limit`` characters, plus the suffix that says how much is missing.

    Returns ``(kept, suffix)``; ``suffix`` is "" when nothing was cut. A cut that would fall
    between the two halves of a surrogate pair moves back one character.
    """
    if len(text) <= limit:
        return text, ""
    end = limit
    if 0 < end < len(text) and "\ud800" <= text[end - 1] <= "\udbff" and "\udc00" <= text[end] <= "\udfff":
        end -= 1
    return text[:end], f"… ({len(text) - end} more characters; {pointer})"
