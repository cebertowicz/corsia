"""Parser-proven redaction for Python source reads.

The text sanitizer decides which characters are sensitive; ``tokenize`` decides
which of them are code. A string's prefix and quotes stay as written, and so
does a ``NAME`` or ``OP`` token unless it overlaps a credential the text
rules recognize by its own shape: a vendor token can tokenize as an attribute
access or a subtraction chain. Every other character inside a sensitive span
is redacted.
"""

from __future__ import annotations

import ast
import bisect
import io
import logging
import re
import tokenize

from host_access.credential_sanitizer import (
    REDACTION_MARKER,
    credential_spans,
)


_BREAK_RE = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85  ]")
logger = logging.getLogger(__name__)


class _TokenPositionError(RuntimeError):
    """tokenize reported a position this module cannot map: a bug, not bad input."""


_LITERAL_RE = re.compile(r"(?is)(?P<prefix>[rubf]{0,3})(?P<quote>'''|\"\"\"|'|\")")


def _line_starts(content: str) -> list[int]:
    """Start offset of each row, split on the LF rows that tokenize uses."""
    starts = [0]
    for line in content.split("\n"):
        starts.append(starts[-1] + len(line) + 1)
    return starts


def _kept_ranges(
    content: str, tokens: list[tokenize.TokenInfo], spans: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Code characters that stay visible inside the sensitive spans."""
    starts = _line_starts(content)
    span_starts = [start for start, _end in spans]
    shapes = credential_spans(content, shape_only=True)
    shape_starts = [start for start, _end in shapes]
    kept: list[tuple[int, int]] = []
    for token in tokens:
        if token.type not in {tokenize.NAME, tokenize.OP, tokenize.STRING}:
            continue
        (row, column), (end_row, end_column) = token.start, token.end
        if not 1 <= row <= len(starts) - 1 or not 1 <= end_row <= len(starts) - 1:
            raise _TokenPositionError("token position is outside physical lines")
        start, end = starts[row - 1] + column, starts[end_row - 1] + end_column
        index = bisect.bisect_right(span_starts, end - 1) - 1
        if index < 0 or spans[index][1] <= start:
            continue
        if token.type != tokenize.STRING:
            shape = bisect.bisect_right(shape_starts, end - 1) - 1
            if shape < 0 or shapes[shape][1] <= start:
                kept.append((start, end))
            continue
        literal = _LITERAL_RE.match(token.string)
        if literal is not None and token.string.endswith(literal.group("quote")):
            kept.append((start, start + literal.end()))
            kept.append((end - len(literal.group("quote")), end))
    return sorted(kept)


def _redacted_runs(
    spans: list[tuple[int, int]], kept: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    ends = [end for _start, end in kept]
    runs: list[tuple[int, int]] = []
    for start, end in spans:
        cursor = start
        for kept_start, kept_end in kept[bisect.bisect_right(ends, start):]:
            if kept_start >= end:
                break
            if kept_start > cursor:
                runs.append((cursor, kept_start))
            cursor = max(cursor, kept_end)
        if cursor < end:
            runs.append((cursor, end))
    merged: list[tuple[int, int]] = []
    for start, end in runs:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def sanitize_python_source(content: str) -> str | None:
    """Redact sensitive spans in Python source while preserving line counts."""
    try:
        ast.parse(content)
        tokens = list(tokenize.generate_tokens(io.StringIO(content).readline))
        spans = credential_spans(content)
        runs = _redacted_runs(spans, _kept_ranges(content, tokens, spans))
    except (SyntaxError, ValueError, tokenize.TokenError):
        return None
    except _TokenPositionError as exc:
        logger.warning("Python read redaction fell back to text rules: %s", exc)
        return None
    output: list[str] = []
    cursor = 0
    for start, end in runs:
        output.append(content[cursor:start])
        output.append(REDACTION_MARKER + "".join(_BREAK_RE.findall(content[start:end])))
        cursor = end
    output.append(content[cursor:])
    result = "".join(output)
    if len(result.splitlines()) != len(content.splitlines()):
        logger.warning("Python read redaction fell back to text rules: line count changed")
        return None
    return result
