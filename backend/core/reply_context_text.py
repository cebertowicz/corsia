"""Escaping for the person's words inside a chat-comment row (render version 1).

``core.reply_context`` puts each comment on one row, ``cid. On #seq: "quote" | note: note``. A quote
is the agent's own text and a note is the person's, so either can hold what looks like a CORSIA
line (``]``, a line break, ``[Reply grounding: ...]``). Rendering escapes them without changing
what is stored.

Escape (``escape_row_text``), lossless and in this order:

- ``\\`` becomes ``\\\\``, then ``"`` becomes ``\\"``, ``[`` becomes ``\\[`` and ``]`` becomes ``\\]``;
- every line break (``\\r\\n``, ``\\r``, ``\\n``, U+0085, U+2028, U+2029) becomes the two characters
  ``\\n``;
- every other C0 control character except tab is dropped.

The backslash goes first so a typed ``\\n`` stays distinguishable from a line break. A row stays one
line and no text in it can open or close a bracketed line or end the quote's double quotes.

Neither the quote nor the note is cut in version 1: a cap needs a measured basis, and there is none
yet. A cap is a later render version (``core.reply_context.REPLY_CONTEXT_RENDER``), never an edit
of this one, because stored rows are re-rendered on every replay.
"""
from __future__ import annotations

import re

_LINE_BREAK = re.compile("\r\n|[\r\n\x85  ]")
# C0 controls other than tab (\x09); \n and \r are already gone by the time this runs.
_DROPPED = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")


def escape_row_text(text: str) -> str:
    """The text as one safe line: see the module docstring for the rules and their order."""
    escaped = (
        text.replace("\\", "\\\\").replace('"', '\\"').replace("[", "\\[").replace("]", "\\]")
    )
    return _DROPPED.sub("", _LINE_BREAK.sub(lambda _: "\\n", escaped))
