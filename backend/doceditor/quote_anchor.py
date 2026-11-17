"""Quote→offset anchor resolver — the load-bearing ergonomic of api-doc-collab.

An agent anchors a doc comment by the *span text* (the ``quote``), never raw
char offsets (contract 1.3). This module resolves a quote against the live doc
content to ``(from_off, to_off)``:

- exactly one occurrence → those offsets;
- repeated span → the occurrence nearest a ``near`` hint (2.2 / 3.3);
- missing or still-ambiguous → raise loudly with candidate line numbers
  (3.3 / 3.4), so the agent re-reads and re-anchors rather than the server
  silently mis-anchoring or dropping the anchor.

Pure functions only (no DB / IO) — the caller supplies the doc text.
"""

from __future__ import annotations

import re


class QuoteResolveError(ValueError):
    """A quote could not be resolved to a single span. Maps to HTTP 422.

    ``candidate_lines`` are 1-based line numbers the agent can use to re-anchor
    (the matches for an ambiguous quote, or best-effort near/keyword lines for a
    missing one).
    """

    kind = "quote_unresolved"

    def __init__(self, message: str, candidate_lines: list[int]):
        super().__init__(message)
        self.message = message
        self.candidate_lines = candidate_lines


class QuoteNotFound(QuoteResolveError):
    """The quote does not occur in the doc (re-read + re-anchor; 3.4)."""

    kind = "quote_not_found"


class AmbiguousQuote(QuoteResolveError):
    """The quote occurs more than once and ``near`` did not disambiguate (3.3)."""

    kind = "quote_ambiguous"


def _line_of(text: str, offset: int) -> int:
    """1-based line number of a character offset."""
    return text.count("\n", 0, offset) + 1


def _occurrences(text: str, sub: str) -> list[int]:
    """All start offsets of ``sub`` in ``text`` (non-overlapping, left-to-right)."""
    offs: list[int] = []
    start = text.find(sub)
    while start != -1:
        offs.append(start)
        # Advance past the whole match — non-overlapping occurrences only, so a
        # self-overlapping span ('aa' in 'aaa') counts once, not twice.
        start = text.find(sub, start + len(sub))
    return offs


def _u16(s: str) -> int:
    """Length of ``s`` in UTF-16 code units — what the browser counts. JS strings
    and DOM selection offsets are UTF-16, and the client offset-remap persists in
    those units, so anchor offsets MUST be UTF-16 (not Python code points) or an
    astral-plane char (emoji, rare CJK) before the span shifts the anchor."""
    return len(s.encode("utf-16-le")) // 2


def _span16(text: str, start_cp: int, quote: str) -> tuple[int, int]:
    """A code-point match start → ``(from_off, to_off)`` in UTF-16 code units."""
    frm = _u16(text[:start_cp])
    return frm, frm + _u16(quote)


def _overlapping_occurrences(text: str, sub: str) -> list[int]:
    """All start offsets, including overlapping occurrences."""
    offs: list[int] = []
    start = text.find(sub)
    while start != -1:
        offs.append(start)
        start = text.find(sub, start + 1)
    return offs


def _with_u16(text: str, positions: list[int]) -> list[tuple[int, int]]:
    """Pair ascending code-point positions with their UTF-16 offsets in one pass."""
    paired: list[tuple[int, int]] = []
    last = last16 = 0
    for pos in positions:
        last16 += _u16(text[last:pos])
        last = pos
        paired.append((pos, last16))
    return paired


def _cp_at_u16(text: str, offset16: object) -> int | None:
    """Return a code-point boundary for a UTF-16 offset, if it is one.

    Decodes the UTF-16 prefix in C rather than walking characters: this runs
    twice per comment on every list, and a per-character loop cost over a
    second on a 200 KB document with 50 comments.
    """
    if not isinstance(offset16, int) or isinstance(offset16, bool) or offset16 < 0:
        return None
    encoded = text.encode("utf-16-le")
    if offset16 * 2 > len(encoded):
        return None
    try:
        return len(encoded[: offset16 * 2].decode("utf-16-le"))
    except UnicodeDecodeError:  # the offset splits a surrogate pair
        return None


def anchor_context(
    text: str, from16: int, to16: int, quote: str | None = None
) -> tuple[str, str] | None:
    """Return up to 32 adjacent characters for a validated UTF-16 span."""
    start = _cp_at_u16(text, from16)
    end = _cp_at_u16(text, to16)
    if start is None or end is None or end < start:
        return None
    if quote is not None and text[start:end] != quote:
        return None
    return text[max(0, start - 32):start], text[end:end + 32]


def reanchor_edited(
    text: str, quote: str, *, from16: int | None, to16: int | None,
    prefix: str | None, suffix: str | None,
) -> tuple[int, int] | None:
    """Recover an edited legacy range when its context and more than half its quote ends survive.

    Only the result is known on a legacy document; keeping a flagged comment is
    a safer error than detaching it, so compare non-overlapping ends in code points.
    """
    if not quote or not prefix or not suffix or from16 is None or to16 is None:
        return None
    if not isinstance(from16, int) or not isinstance(to16, int):
        return None
    starts = _with_u16(text, [pos + len(prefix) for pos in _overlapping_occurrences(text, prefix)])
    ends = _with_u16(text, _overlapping_occurrences(text, suffix))
    if not starts or not ends:
        return None
    start_distance = min(abs(pos16 - from16) for _, pos16 in starts)
    end_distance = min(abs(pos16 - to16) for _, pos16 in ends)
    starts = [(pos, pos16) for pos, pos16 in starts if abs(pos16 - from16) == start_distance]
    ends = [(pos, pos16) for pos, pos16 in ends if abs(pos16 - to16) == end_distance]
    if len(starts) != 1 or len(ends) != 1:
        return None
    start, start16 = starts[0]
    end, end16 = ends[0]
    if end <= start:
        return None
    inside = text[start:end]
    leading = 0
    while leading < min(len(quote), len(inside)) and quote[leading] == inside[leading]:
        leading += 1
    trailing = 0
    while (trailing < min(len(quote), len(inside)) - leading
           and quote[-trailing - 1] == inside[-trailing - 1]):
        trailing += 1
    if 2 * (leading + trailing) <= len(quote):
        return None
    return start16, end16


def _is_neighbour_line(text: str, start: int, end: int, prefix: str | None, suffix: str | None) -> bool:
    """Whether the quote at ``start``/``end`` sits on the line beside the commented one, not on the commented one.

    A comment whose line was deleted finds the same words on the line that moved into its place: that line
    starts like the deleted one and so matches one stored side. The stored context says whose line it is.
    The text after the commented line's first line break (blank lines skipped) is the start of the line below
    it; if that starts this line, and does not start the next non-blank line after this one, this is the line
    below, moved up. The text before the last line break is the end of the line above it; the same test,
    mirrored, finds the line above.
    It never refuses a line whose own text around the quote is unchanged: that is the commented line,
    whatever happened to its neighbours.

    Limit: it needs a line break inside the stored context (up to 32 characters each side), so a comment
    inside a long line has none and the caller's matching rule stands unchanged. When the neighbouring
    line was edited in the same rewrite its stored start no longer matches and this cannot tell it apart
    from the commented line itself edited.
    """
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line_end = len(text) if line_end < 0 else line_end
    before_in_line = (prefix or "").rsplit("\n", 1)[-1]
    after_in_line = (suffix or "").split("\n", 1)[0]
    if (before_in_line or after_in_line) and text[max(0, start - len(before_in_line)):start] == before_in_line \
            and text[end:end + len(after_in_line)] == after_in_line:
        return False
    # Blank lines between items are skipped on both sides, so paragraphs and loose lists work too.
    next_start = line_end + 1
    while next_start < len(text) and text[next_start] == "\n":
        next_start += 1
    prev_end = max(line_start - 1, 0)
    while prev_end > 0 and text[prev_end - 1] == "\n":
        prev_end -= 1
    if suffix and "\n" in suffix:
        below = suffix.split("\n", 1)[1].lstrip("\n")
        if below and text.startswith(below, line_start) and not text.startswith(below, next_start):
            return True
    if prefix and "\n" in prefix:
        above = prefix.rsplit("\n", 1)[0].rstrip("\n")
        if above and text.endswith(above, 0, line_end) and not text.endswith(above, 0, prev_end):
            return True
    return False


def reanchor(
    text: str,
    quote: str,
    *,
    from16: int | None,
    to16: int | None,
    prefix: str | None,
    suffix: str | None,
) -> tuple[int, int] | None:
    """Find ``quote`` using its adjacent context and UTF-16 position hint.

    A candidate needs matching stored context, unless it remains at the stored
    span. A comment with no stored context can only stay at its stored span:
    nothing tells its original quote apart from another occurrence, so any
    move could attach it to different text. Ties at the same score and distance
    deliberately become orphaned for the same reason.

    Two cases tighten that, so a reload agrees with the live document. A stored span that is collapsed
    (the comment's text is gone) comes back only where every stored side matches, as when deleted text
    is restored. A candidate that is the line beside the commented one is refused
    (``_is_neighbour_line``), at its stored place too.
    """
    if not quote:
        return None
    candidates = [
        (start, *_span16(text, start, quote))
        for start in _overlapping_occurrences(text, quote)
    ]
    if not candidates:
        return None

    eligible: list[tuple[int, int, int, int]] = []
    for start, candidate_from, candidate_to in candidates:
        score = sides = 0
        if prefix:
            sides += 1
            score += int(text[max(0, start - len(prefix)):start] == prefix)
        end = start + len(quote)
        if suffix:
            sides += 1
            score += int(text[end:end + len(suffix)] == suffix)
        exact = candidate_from == from16 and candidate_to == to16
        if from16 is not None and from16 == to16:
            counts = sides > 0 and score == sides
        else:
            counts = score > 0 or exact
        if counts and not _is_neighbour_line(text, start, end, prefix, suffix):
            eligible.append((score, start, candidate_from, candidate_to))
    if not eligible:
        return None

    best_score = max(candidate[0] for candidate in eligible)
    best = [candidate for candidate in eligible if candidate[0] == best_score]
    hint = from16 if from16 is not None else 0
    nearest = min(abs(candidate[2] - hint) for candidate in best)
    winners = [candidate for candidate in best if abs(candidate[2] - hint) == nearest]
    if len(winners) != 1:
        return None
    _, _start, candidate_from, candidate_to = winners[0]
    return candidate_from, candidate_to


def _missing_candidates(text: str, quote: str, near: str | None) -> list[int]:
    """Best-effort line hints for a quote that did not match (loud, not silent).

    Prefer the ``near`` hint's lines; otherwise the lines containing the
    quote's longest word. Empty when nothing plausible is found.
    """
    if near:
        near_lines = sorted({_line_of(text, o) for o in _occurrences(text, near)})
        if near_lines:
            return near_lines
    words = sorted(re.findall(r"\w+", quote), key=len, reverse=True)
    for w in words:
        if len(w) <= 3:
            break
        hits = sorted({_line_of(text, o) for o in _occurrences(text, w)})
        if hits:
            return hits
    return []


def resolve_quote(text: str, quote: str, near: str | None = None) -> tuple[int, int]:
    """Resolve ``quote`` to ``(from_off, to_off)`` against ``text``, in UTF-16
    code units (the browser's offset basis — see ``_u16``).

    Raises ``QuoteNotFound`` / ``AmbiguousQuote`` (both carry
    ``candidate_lines``) on a miss or unresolved duplicate, and ``ValueError``
    on an empty quote (a caller bug — the route validates first).
    """
    if not quote:
        raise ValueError("quote must be a non-empty span")

    matches = _occurrences(text, quote)
    if len(matches) == 1:
        return _span16(text, matches[0], quote)
    if not matches:
        raise QuoteNotFound(
            f"quote not found in the doc: {quote!r} — re-read and re-anchor",
            _missing_candidates(text, quote, near),
        )

    # Repeated span — disambiguate by proximity to ``near``.
    match_lines = sorted({_line_of(text, m) for m in matches})
    if not near:
        raise AmbiguousQuote(
            f"quote occurs {len(matches)}× — pass `near` to disambiguate "
            f"(candidate lines: {match_lines})",
            match_lines,
        )

    near_offs = _occurrences(text, near)
    if not near_offs:
        raise AmbiguousQuote(
            f"quote occurs {len(matches)}× and `near` ({near!r}) is not in the "
            f"doc — re-read and re-anchor (candidate lines: {match_lines})",
            match_lines,
        )

    def _dist(m: int) -> int:
        return min(abs(m - n) for n in near_offs)

    best = min(_dist(m) for m in matches)
    closest = [m for m in matches if _dist(m) == best]
    if len(closest) != 1:
        raise AmbiguousQuote(
            f"quote occurs {len(matches)}× and `near` is equidistant — "
            f"refine `near` (candidate lines: {match_lines})",
            match_lines,
        )
    return _span16(text, closest[0], quote)
