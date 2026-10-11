"""Grep's content-mode lines: assembly, and the same redaction ``read_file`` applies.

A content line from a file with a source path shows what ``read_file`` shows
for that line. The file is read once and its credential spans are found on the
whole text, so a multi-line private key stays covered. When a span meets an
emitted line, the whole file goes through ``sanitize_source`` and the line is
taken from its result; a line no span meets is unchanged on both paths. That
holds only while the printed text still equals the file's line at that number,
and only within a per-call time budget. Every other line takes the text rules,
applied to each run of such lines joined as the search printed them, before
the result is marked safe.
"""

from __future__ import annotations

import bisect
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import time

from host_access.credential_sanitizer import (
    CredentialSafeText,
    credential_spans,
    sanitize_credential_text,
)
from host_access.source_redaction import count_fallback, has_source_path, sanitize_source
from tools.filesystem_binary import open_bound_binary

BUDGET_SECONDS = 1.0
_MAX_FILE_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class GrepLine:
    """One content line as the search printed it: its path prefix, text and number."""

    prefix: str
    text: str
    number: int = 0


SEPARATOR = GrepLine("--", "")


def context_ranges(matching: list[int], context: int, total: int) -> list[tuple[int, int]]:
    """Merged ``[start, end)`` line-index windows around each matching index."""
    ranges: list[tuple[int, int]] = []
    for index in matching:
        start = max(0, index - context)
        end = min(total, index + context + 1)
        if ranges and start <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
        else:
            ranges.append((start, end))
    return ranges


def read_source(path: Path) -> str | None:
    """One bound read of a matched file, under ``read_file``'s size cap."""
    try:
        with open_bound_binary(path) as (handle, file_stat):
            if file_stat.st_size > _MAX_FILE_BYTES:
                count_fallback("size")
                return None
            data = handle.read()
    except OSError:
        count_fallback("grep_line_mismatch", detail="matched file could not be read")
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        count_fallback("roundtrip", detail="matched file is not UTF-8")
        return None


def _rows(content: str, universal: bool) -> tuple[list[str], list[int]]:
    """Lines as the search numbered them, and each line's start offset."""
    if universal:
        rows, ended = content.splitlines(), content.splitlines(keepends=True)
    else:
        rows = content.split("\n")
        ended = [f"{row}\n" for row in rows]
    starts = [0]
    for row in ended:
        starts.append(starts[-1] + len(row))
    return rows, starts


class SourceLines:
    """Source-path substitution for one grep call, under a time budget."""

    def __init__(self, budget_seconds: float | None = None) -> None:
        self._budget = BUDGET_SECONDS if budget_seconds is None else budget_seconds
        self._spent = 0.0

    def substitute(
        self,
        suffix: str,
        source: str | Callable[[], str | None],
        emitted: list[GrepLine],
        *,
        universal: bool = False,
    ) -> list[str | None]:
        """Faithful text for each emitted line of one file; ``None`` takes the text rules.

        ``source`` is the file's text, or reads it. ``universal`` numbers lines
        as ``str.splitlines`` does; otherwise on LF.
        """
        if not emitted or not has_source_path(suffix):
            return [None] * len(emitted)
        if self._spent >= self._budget:
            for _line in emitted:
                count_fallback("grep_budget")
            return [None] * len(emitted)
        started = time.perf_counter()
        try:
            content = source if isinstance(source, str) else source()
            if content is None:
                return [None] * len(emitted)
            return self._substitute(suffix, content, emitted, universal)
        finally:
            self._spent += time.perf_counter() - started

    @staticmethod
    def _substitute(
        suffix: str, content: str, emitted: list[GrepLine], universal: bool,
    ) -> list[str | None]:
        rows, starts = _rows(content, universal)
        spans = credential_spans(content)
        span_starts = [start for start, _end in spans]
        result: list[str | None] = [None] * len(emitted)
        lexed: list[int] = []
        for position, line in enumerate(emitted):
            if line is SEPARATOR:
                continue
            if not 1 <= line.number <= len(rows) or rows[line.number - 1] != line.text:
                count_fallback("grep_line_mismatch")
                continue
            # A line's range includes its break: a run that starts there puts
            # its marker at the end of this line.
            start, end = starts[line.number - 1], starts[line.number]
            index = bisect.bisect_right(span_starts, end - 1) - 1
            if index >= 0 and spans[index][1] > start:
                lexed.append(position)
            else:
                result[position] = line.text
        if not lexed:
            return result
        safe = sanitize_source(content, suffix)
        safe_rows = None if safe is None else _rows(safe, universal)[0]
        if safe_rows is not None and len(safe_rows) != len(rows):
            count_fallback("linecount", warn=True)
            safe_rows = None
        if safe_rows is not None:
            for position in lexed:
                result[position] = safe_rows[emitted[position].number - 1]
        return result


def render(
    lines: list[GrepLine], safe: list[str | None], output_cap: int, header: str = "",
) -> CredentialSafeText:
    """Join content lines; each run of lines without faithful text takes the text rules.

    ``header`` leads the output, outside the cap.
    """
    chunks: list[str] = []
    pending: list[str] = []
    for line, text in zip(lines, safe, strict=True):
        if text is None:
            pending.append(f"{line.prefix}{line.text}")
            continue
        if pending:
            chunks.append(sanitize_credential_text("\n".join(pending)))
            pending = []
        chunks.append(f"{sanitize_credential_text(line.prefix)}{text}")
    if pending:
        chunks.append(sanitize_credential_text("\n".join(pending)))
    rendered = "\n".join(chunks)
    if len(rendered) > output_cap:
        rendered = (
            rendered[:output_cap]
            + f"\n\n... (output truncated at {output_cap:,} chars. Narrow your search "
            "with glob filters or use 'files' mode.)"
        )
    return CredentialSafeText(sanitize_credential_text(header) + rendered)
