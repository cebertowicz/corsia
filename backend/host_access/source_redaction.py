"""Parser-proven redaction for source reads.

The text sanitizer decides which characters are sensitive; a lexer proven for
the file decides which of them are code. Python uses ``ast`` and ``tokenize``;
each other mapped extension uses one named Pygments lexer. Inside a sensitive
span, a name, keyword, operator or punctuation token stays as written unless
the text rules would change its own text or it overlaps a credential they
recognize by its shape, URL userinfo or a cookie value: a vendor token can lex
as a name, and a Markdown link target is one. A string keeps its prefix and
matching quotes. In Markdown prose and in comments, a value after a credential
name that is one plain lowercase word is English and stays. Every other
character inside a span is redacted. Any doubt sends the whole file to the
text rules, counted by reason.
"""

from __future__ import annotations

import ast
import bisect
import collections
from dataclasses import dataclass
import io
import logging
import re
import tokenize

from pygments.lexer import Lexer
from pygments.lexers import (
    BashLexer, JavascriptLexer, JsonLexer, MarkdownLexer, TOMLLexer, TsxLexer,
    TypeScriptLexer, YamlLexer,
)
from pygments.token import Comment, Error, Keyword, Name, Operator, Punctuation, String, Text

from host_access.credential_sanitizer import (
    REDACTION_MARKER,
    credential_spans,
    sanitize_credential_text,
)


_BREAK_RE = re.compile(r"\r\n|[\n\r\v\f\x1c-\x1e\x85  ]")
_LITERAL_RE = re.compile(r"(?is)(?P<prefix>[rubf]{0,3})(?P<quote>'''|\"\"\"|'|\")")
_PROSE_WORD_RE = re.compile(r"[a-z]{1,15}[.,;:]?")
_QUOTES = "\"'`"
_PYTHON_SUFFIXES = frozenset({".py", ".pyi"})
# A closed map: an extension not named here keeps the text rules.
_LEXERS: dict[str, type[Lexer]] = {
    ".ts": TypeScriptLexer, ".tsx": TsxLexer, ".js": JavascriptLexer, ".jsx": TsxLexer,
    ".mjs": JavascriptLexer, ".cjs": JavascriptLexer, ".sh": BashLexer, ".bash": BashLexer,
    ".zsh": BashLexer, ".yaml": YamlLexer, ".yml": YamlLexer, ".json": JsonLexer,
    ".toml": TOMLLexer, ".md": MarkdownLexer,
}
# Regex lexers grow with their input; past this a read takes the text rules.
MAX_LEXED_CHARS = 1_000_000
FALLBACKS: collections.Counter[str] = collections.Counter()
logger = logging.getLogger(__name__)


class _Fallback(Exception):
    """The lexer is not proven for this file; ``reason`` names why.

    ``warn`` marks a failure that is a bug in this module, not unusual input.
    """

    def __init__(self, reason: str, detail: str = "", *, warn: bool = False) -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.warn = warn


@dataclass(frozen=True)
class _Piece:
    """One token that may stay visible inside a span, and what of it may."""

    start: int
    end: int
    kind: str  # "code", "string" or "prose"
    delimiters: tuple[tuple[int, int], ...] = ()


def has_source_path(suffix: str) -> bool:
    """Whether reads of files with this extension can take the source path."""
    return suffix.casefold() in _PYTHON_SUFFIXES or suffix.casefold() in _LEXERS


def count_fallback(reason: str, *, detail: str = "", warn: bool = False) -> None:
    """Count one fail-closed fallback; the log line carries no content or path."""
    FALLBACKS[reason] += 1
    log = logger.warning if warn else logger.info
    log("Source redaction fell back to text rules: %s", detail or reason)


def _line_starts(content: str) -> list[int]:
    """Start offset of each row, split on the LF rows that tokenize uses."""
    starts = [0]
    for line in content.split("\n"):
        starts.append(starts[-1] + len(line) + 1)
    return starts


def _python_pieces(content: str) -> list[_Piece]:
    ast.parse(content)
    starts = _line_starts(content)
    pieces: list[_Piece] = []
    for token in tokenize.generate_tokens(io.StringIO(content).readline):
        if token.type not in {tokenize.NAME, tokenize.OP, tokenize.STRING}:
            continue
        (row, column), (end_row, end_column) = token.start, token.end
        if not 1 <= row <= len(starts) - 1 or not 1 <= end_row <= len(starts) - 1:
            raise _Fallback("roundtrip", "token position is outside physical lines", warn=True)
        start, end = starts[row - 1] + column, starts[end_row - 1] + end_column
        if token.type != tokenize.STRING:
            pieces.append(_Piece(start, end, "code"))
            continue
        literal = _LITERAL_RE.match(token.string)
        if literal is not None and token.string.endswith(literal.group("quote")):
            closing = end - len(literal.group("quote"))
            pieces.append(
                _Piece(start, end, "string", ((start, start + literal.end()), (closing, end)))
            )
    return pieces


def _string_delimiters(start: int, value: str) -> tuple[tuple[int, int], ...]:
    """A string token's own matching quotes: all of a bare delimiter, else both ends."""
    quote, end = value[:1], start + len(value)
    if not quote or quote not in _QUOTES:
        return ()
    if value == quote * len(value):
        return ((start, end),)
    width = 3 if len(value) >= 6 and value.startswith(quote * 3) and value.endswith(quote * 3) else 1
    if len(value) >= 2 and value.endswith(quote * width):
        return ((start, start + width), (end - width, end))
    return ()


def _lexer_pieces(content: str, lexer_class: type[Lexer]) -> list[_Piece]:
    if len(content) > MAX_LEXED_CHARS:
        raise _Fallback("size")
    lexer = lexer_class(stripnl=False, ensurenl=False)
    prose_text = lexer_class is MarkdownLexer
    pieces: list[_Piece] = []
    offset = 0
    for kind, value in lexer.get_tokens(content):
        start, offset = offset, offset + len(value)
        if kind in Error:
            raise _Fallback("error_token")
        if content[start:offset] != value:
            raise _Fallback("roundtrip")
        if kind in Name or kind in Keyword or kind in Operator or kind in Punctuation:
            pieces.append(_Piece(start, offset, "code"))
        elif kind in String:
            pieces.append(_Piece(start, offset, "string", _string_delimiters(start, value)))
        elif kind in Comment or (prose_text and kind in Text):
            pieces.append(_Piece(start, offset, "prose"))
    if offset != len(content):
        raise _Fallback("roundtrip")
    return pieces


def _overlaps(ranges: list[tuple[int, int]], starts: list[int], start: int, end: int) -> bool:
    index = bisect.bisect_right(starts, end - 1) - 1
    return index >= 0 and ranges[index][1] > start


def _all_prose(prose: list[_Piece], starts: list[int], start: int, end: int) -> bool:
    """Whether contiguous prose tokens cover every character of ``[start, end)``."""
    index = bisect.bisect_right(starts, start) - 1
    cursor = start
    while 0 <= index < len(prose) and prose[index].start <= cursor < prose[index].end:
        cursor = prose[index].end
        if cursor >= end:
            return True
        index += 1
    return False


def _kept_ranges(
    content: str, pieces: list[_Piece], spans: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """Code characters, quotes and prose words that stay visible inside the spans."""
    span_starts = [start for start, _end in spans]
    never = credential_spans(content, never_code=True)
    never_starts = [start for start, _end in never]
    kept: list[tuple[int, int]] = []
    for piece in pieces:
        if not _overlaps(spans, span_starts, piece.start, piece.end):
            continue
        if piece.kind == "string":
            kept.extend(piece.delimiters)
        elif piece.kind == "code":
            text = content[piece.start:piece.end]
            if (
                not _overlaps(never, never_starts, piece.start, piece.end)
                and sanitize_credential_text(text) == text
            ):
                kept.append((piece.start, piece.end))
    prose = [piece for piece in pieces if piece.kind == "prose"]
    prose_starts = [piece.start for piece in prose]
    for start, end in spans:
        if (
            _PROSE_WORD_RE.fullmatch(content, start, end)
            and not _overlaps(never, never_starts, start, end)
            and _all_prose(prose, prose_starts, start, end)
        ):
            kept.append((start, end))
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


def sanitize_source(content: str, suffix: str) -> str | None:
    """Redact sensitive spans in source, preserving line counts.

    ``None`` means the text rules apply: the extension has no source path, or
    its lexer failed to prove the file.
    """
    suffix = suffix.casefold()
    try:
        if suffix in _PYTHON_SUFFIXES:
            pieces = _python_pieces(content)
        elif suffix in _LEXERS:
            pieces = _lexer_pieces(content, _LEXERS[suffix])
        else:
            return None
        spans = credential_spans(content)
        runs = _redacted_runs(spans, _kept_ranges(content, pieces, spans))
    except (SyntaxError, ValueError, tokenize.TokenError):
        count_fallback("error_token")
        return None
    except _Fallback as exc:
        count_fallback(exc.reason, detail=str(exc), warn=exc.warn)
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
        count_fallback("linecount", warn=True)
        return None
    return result
