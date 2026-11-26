"""Decide whether a diff-mapped document comment still has a credible place.

This only evaluates a candidate; callers retain the stored quote and body when
it returns false. Offsets are UTF-16, matching document comments.
"""
import re
from bisect import bisect_left
from collections import Counter

from doceditor import live_diff
class TextIndex:
    """Resolution-local offsets and line positions for an immutable string."""

    def __init__(self, text: str):
        self.text = text
        self._encoded: bytes | None = None
        self._positions: dict[int, int | None] = {}
        self._newlines: list[int] | None = None

    def cp_at_u16(self, offset: object) -> int | None:
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            return None
        if offset not in self._positions:
            if self._encoded is None:
                self._encoded = self.text.encode('utf-16-le')
            if offset * 2 > len(self._encoded):
                result = None
            else:
                try:
                    result = len(self._encoded[:offset * 2].decode('utf-16-le'))
                except UnicodeDecodeError:
                    result = None
            self._positions[offset] = result
        return self._positions[offset]

    def line_at(self, position: int) -> int:
        if self._newlines is None:
            self._newlines = [match.start() for match in re.finditer('\n', self.text)]
        return bisect_left(self._newlines, position)


def _unnumber(line: str) -> str:
    line = re.sub(r'^(\s*\|\s*)\d+(\s*\|)', r'\1#\2', line)
    return re.sub(r'^(\s*)\d+([.)]\s+)', r'\1#\2', line)


def _line_pairs(old: str, new: str, *, new_lines=None, new_positions=None) -> dict[int, tuple[str, int | None]]:
    before = old.splitlines(keepends=True)
    after = new.splitlines(keepends=True) if new_lines is None else new_lines
    result = {}
    for tag, i, j, k, end in live_diff._line_opcodes(before, after, positions=new_positions):
        if tag == 'equal':
            for n in range(j - i):
                result[i + n] = ('equal', k + n)
        elif tag == 'delete':
            for n in range(i, j):
                result[n] = ('delete', None)
        elif tag == 'replace':
            left, right = before[i:j], after[k:end]
            several = len(left) > 1 or len(right) > 1
            if several and (len(left) * len(right) > live_diff.MAX_LINE_PAIRS
                            or live_diff._pair_work(left, right) > live_diff.MAX_PAIR_WORK):
                for n in range(i, j):
                    result[n] = ('unclear', None)
                continue
            x, y = i, k
            for old_group, new_group in live_diff._pair_lines(left, right):
                nl = len(old_group.splitlines(keepends=True)) if old_group is not None else 0
                nr = len(new_group.splitlines(keepends=True)) if new_group is not None else 0
                for n in range(nl):
                    result[x + n] = ('paired', y) if nl == nr == 1 else ('unclear', None)
                x, y = x + nl, y + nr
            old_keys, new_keys = list(map(_unnumber, left)), list(map(_unnumber, right))
            for n, key in enumerate(old_keys):
                if key != left[n] and old_keys.count(key) == new_keys.count(key) == 1:
                    result[i + n] = ('paired', k + new_keys.index(key))
    return result


def _fragments(text: str, start: int, end: int) -> tuple[str, str]:
    lo = text.rfind('\n', 0, start) + 1
    hi = text.find('\n', end)
    if hi < 0:
        hi = len(text)
    return text[max(lo, start - 32):start], text[end:min(hi, end + 32)]


def _ambiguous_duplicate(old: str, new: str, start: int, end: int, quote: str, *, line_counts=None) -> bool:
    line_start = old.rfind('\n', 0, start) + 1
    line_end = old.find('\n', end)
    if line_end < 0:
        line_end = len(old)
    line = old[line_start:line_end]
    old_counts, new_counts = line_counts if line_counts is not None else (Counter(old.splitlines()), Counter(new.splitlines()))
    if not old_counts[line] > new_counts[line] > 0:
        return False
    matches, offset = 0, 0
    for item in new.splitlines(keepends=True):
        if item.rstrip('\r\n') == line:
            pos = offset + start - line_start
            qend = pos + len(quote)
            if (new[max(0, pos - 32):pos], new[qend:qend + 32]) == (
                    old[max(0, start - 32):start], old[end:end + 32]):
                matches += 1
        offset += len(item)
    return matches != 1


def _word_content(fragment: str) -> int:
    fragment = re.sub(r'^\s*(?:(?:[-+*]|\d+[.)])\s*)+', '', fragment)
    return sum(char.isalnum() for char in fragment)


def evaluate(old: str, new: str, old_span: tuple[int, int], new_span: tuple[int, int],
             quote: str, *, line_pairs: dict | None = None, line_counts=None,
             old_index: TextIndex | None = None, new_index: TextIndex | None = None) -> tuple[bool, bool, str]:
    """Return (may show, duplicate ambiguity, reason) for the actual mapped span."""
    old_index = old_index or TextIndex(old)
    new_index = new_index or TextIndex(new)
    a, b = (old_index.cp_at_u16(pos) for pos in old_span)
    c, d = (new_index.cp_at_u16(pos) for pos in new_span)
    if a is None or b is None or c is None or d is None or b < a or d <= c:
        return False, False, 'invalid_or_collapsed'
    if _ambiguous_duplicate(old, new, a, b, quote, line_counts=line_counts):
        return False, True, 'duplicate_ambiguous'
    old_line = old_index.line_at(a)
    new_start, new_end = new_index.line_at(c), new_index.line_at(d - 1)
    pairs = _line_pairs(old, new) if line_pairs is None else line_pairs
    kind, target = pairs.get(old_line, ('unclear', None))
    if '\n' in old[a:b]:
        end_kind, end_target = pairs.get(old_index.line_at(b - 1), ('unclear', None))
        if (kind not in ('equal', 'paired') or target != new_start
                or end_kind not in ('equal', 'paired') or end_target != new_end):
            return False, False, 'line_' + kind
        if old != new:
            old_count, new_count = old.count(quote), new.count(quote)
            if old_count != new_count and max(old_count, new_count) > 1 and min(old_count, new_count) > 0:
                return False, True, 'duplicate_ambiguous'
        before, after = _fragments(old, a, b)
        current_before, current_after = _fragments(new, c, d)
        return (new[c:d] == quote and before == current_before and after == current_after), False, 'context'
    if kind in ('delete', 'unclear') or target != new_start or new_start != new_end:
        return False, False, 'line_' + kind
    if kind == 'paired':
        return True, False, 'paired_edit'
    before, after = _fragments(old, a, b)
    current_before, current_after = _fragments(new, c, d)
    left, right = before == current_before, after == current_after
    strong = (left and _word_content(before) >= 8) or (right and _word_content(after) >= 8)
    if new[c:d] != quote:
        return False, False, 'quote_changed_equal'
    return bool(strong or left and right), False, 'context'
